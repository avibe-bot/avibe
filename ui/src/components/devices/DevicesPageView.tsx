// Settings · Devices (ui/design: settings/devices/*): the one page for remote access, the devices
// that reach this Avibe, what each offers, and recent activity. Three layers: how this Avibe is
// reached (set once), the devices (one row each, a detail with its functions and one way out), and
// recent activity. No pairing key anywhere: remote access uses the sign-in at avibe.bot.
// A view: props only. `DevicesPage` reads the APIs, maps them onto `./types`, and passes handlers.
import type { ReactNode } from 'react';
import {
  AlarmClock, Bell, Check, ChevronRight, Clock, Copy, Download, ExternalLink, Globe, HeartPulse, Laptop, LoaderCircle,
  LogOut, Monitor, MonitorCog, MonitorSmartphone, Plus, RefreshCw, Smartphone, X,
} from 'lucide-react';
import { QRCodeSVG } from 'qrcode.react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import type { TranslationKey } from '@/i18n/types';
import { SettingsPageShell } from '../settings/SettingsPageShell';
import { SettingsPanel, ToggleSwitch } from '../settings/SettingsPrimitives';
import { StatusDot } from '../SidebarStatusRow';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '../ui/dialog';
import { KeptTail } from '../ui/kept-tail';
import { SegmentedRadio } from '../ui/segmented';
import { ClaimFields } from './claim';
import { OpenNewAddress } from './OpenNewAddress';
import { clientText, connectionText, deviceName, deviceTime, healthText, joinParts, timeParts, upperFirst } from './format';
import { BackToDevices, Confirm, IconTile, Notice, OptimizeRow } from './parts';
import { claimProblemText } from './texts';
import {
  RECENT_ACTIVITY_LIMIT, TESTFLIGHT_INVITE_URL, accountResultTexts, activityText, functionControl, instanceUrl, lockedOutCount,
  newestFirst, othersSignedIn, remoteDevices, signOutNeedsConfirm,
} from './rules';
import type {
  AccountResult, AddKind, Device, DeviceActivity, DeviceFunction, DevicesConfirm, FunctionKind, OptimizeState, Reach, SignOutNotice,
  SignedOutDevice, UnlinkedHealth,
} from './types';

const functionKeys = {
  notifications: 'devices.capability.notifications',
  health: 'devices.capability.health',
  schedule: 'devices.capability.schedule',
  computerUse: 'devices.capability.computerUse',
} as const satisfies Record<FunctionKind, TranslationKey>;
const functionIcon = { notifications: Bell, health: HeartPulse, schedule: AlarmClock, computerUse: MonitorCog } as const;
const deviceIcon = { host: Laptop, iphone: Smartphone, desktop: Monitor, browser: Globe, other: MonitorSmartphone } as const;

export type DevicesPageActions = {
  onTurnOn?: () => void;
  /** Cancel the open claim: this page's own, or by its id the one another tab of this computer left. */
  onCancelClaim?: (claimId?: string) => void;
  onSignIn?: () => void;
  onNameChange?: (name: string) => void;
  onRemoteAccessChange?: (on: boolean) => void;
  onRetry?: () => void;
  onCopyAddress?: () => void;
  onOpenAddress?: () => void;
  onOpenConnection?: () => void;
  onOptimizeRoute?: () => void;
  /** To the bind-again row of the connection page. */
  onOpenRebind?: () => void;
  onOpenDevice?: (id: string) => void;
  onCloseDevice?: () => void;
  onFunctionChange?: (deviceId: string, kind: FunctionKind, on: boolean) => void;
  onSignOutDevice?: (id: string) => void;
  onSignOutOthers?: () => void;
  onDisconnectHealth?: (id: string) => void;
  onConfirm?: () => void;
  onCancelConfirm?: () => void;
  onDismissNotice?: () => void;
  onShowActivity?: () => void;
  /** Read the next page of older activity. */
  onMoreActivity?: () => void;
  onAddDevice?: () => void;
  onAddKindChange?: (kind: AddKind) => void;
  onCloseAdd?: () => void;
  onOpenDownloads?: () => void;
  /** Copy the add-a-device link (the same address as its code). */
  onCopyLink?: (url: string) => void;
  /** Back from the activity list to the page. */
  onBack?: () => void;
};

export type DevicesPageViewProps = DevicesPageActions & {
  reach: Reach;
  devices: Device[];
  activity?: DeviceActivity[];
  /** The activity list has older entries the instance still keeps: offered, or being read. */
  moreActivity?: 'available' | 'loading';
  /** The device list as read: being read for the first time, or not readable. Absent once read. */
  list?: 'loading' | 'unknown';
  /** A member of a shared instance sees only their own devices and nothing of the owner's. */
  viewer?: 'owner' | 'member';
  /** The viewer is a member who may operate the instance: they keep the remote-access switch and
   *  「优化线路」. Binding, the domain, the connection settings and other people's devices stay the
   *  owner's. */
  operate?: boolean;
  optimize?: OptimizeState;
  /** The account the person looking is signed in as, when the page knows it. */
  viewerAccount?: string;
  /** What signing out in bulk covers here: the viewer's other devices, or, at the computer itself,
   *  every remote device. Null: nothing to sign out in bulk. */
  bulk?: 'other_sessions' | 'all_remote' | null;
  unlinkedHealth?: UnlinkedHealth[];
  /** Devices signed out lately, each with what avibe.bot did about its account sign-in. */
  signedOut?: SignedOutDevice[];
  /** The answer of a sign-out just made on this page. */
  signOutNotice?: SignOutNotice;
  /** The address was changed somewhere else, with this computer's device key. */
  unrecognizedChange?: { hostname: string };
  /** Something this page asked for did not happen, or its answer never arrived. `offFromAfar`:
   *  remote access was turned off from this remote device and no answer came back, which is what
   *  turning it off does. */
  problem?: 'functionNeedsDevice' | 'failed' | 'gone' | 'lost' | 'offFromAfar';
  /** The device whose detail is open. */
  detail?: string;
  /** A way out was pressed and something would be lost: the confirmation. */
  confirm?: DevicesConfirm;
  /** A confirmed action is being carried out. */
  busy?: boolean;
  /** The full activity list in place of the page. */
  view?: 'page' | 'activity';
  /** 「添加设备」 on one of its tabs: inline while this computer is the only device, else a dialog. */
  add?: AddKind;
};

const noop = () => undefined;

const problemKeys = {
  functionNeedsDevice: 'devices.problem.functionNeedsDevice',
  failed: 'devices.problem.failed',
  gone: 'devices.problem.gone',
  lost: 'devices.problem.lost',
  offFromAfar: 'devices.problem.offFromAfar',
} as const satisfies Record<NonNullable<DevicesPageViewProps['problem']>, TranslationKey>;

const healthSyncedKeys = {
  today: 'devices.capability.healthSyncedToday',
  yesterday: 'devices.capability.healthSyncedYesterday',
  other: 'devices.capability.healthSyncedDated',
} as const satisfies Record<NonNullable<ReturnType<typeof timeParts>>['day'], TranslationKey>;

/** Layer 1: how this Avibe is reached. Set once, so it is compact once it is on. */
function ReachCard({ reach, operate = false, optimize, actions }: {
  reach: Reach;
  /** A member who may operate the instance: the switch and 「优化线路」, nothing of the owner's. */
  operate?: boolean;
  optimize?: OptimizeState;
  actions: DevicesPageActions;
}) {
  const { t } = useTranslation();
  const offBadge = <Badge variant="secondary">{t('devices.reach.off')}</Badge>;
  if (reach.state === 'unknown') {
    return (
      <SettingsPanel title={t('devices.reach.title')}>
        <div role="alert" className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center md:justify-between">
          <p className="text-[12px] leading-relaxed text-foreground">{t('devices.reach.unknown')}</p>
          <Button variant="secondary" size="xs" onClick={actions.onRetry}><RefreshCw />{t('common.retry')}</Button>
        </div>
      </SettingsPanel>
    );
  }
  if (reach.state === 'moved') return <OpenNewAddress host={reach.host} onCopy={actions.onCopyLink} />;
  if (reach.state === 'loading') {
    return (
      <SettingsPanel title={t('devices.reach.title')}>
        <div role="status" className="flex items-center gap-2.5 px-5 py-4 text-[13px] text-muted">
          <LoaderCircle className="size-4 motion-safe:animate-spin" />
          {t('common.loading')}
        </div>
      </SettingsPanel>
    );
  }
  if (reach.state === 'starting') {
    return (
      <SettingsPanel title={t('devices.reach.title')} description={t('devices.reach.lead')}>
        <div className="flex items-center gap-3 px-5 py-4">
          <div role="status" className="flex min-w-0 flex-1 items-center gap-2.5 text-[13px] text-foreground">
            <LoaderCircle className="size-4 shrink-0 text-muted motion-safe:animate-spin" />
            {t(reach.retrying ? 'devices.reach.retrying' : reach.waiting ? 'devices.reach.waiting' : 'devices.reach.starting')}
          </div>
          {(reach.waiting || reach.retrying) && <Button variant="ghost" size="xs" disabled={reach.busy} onClick={() => actions.onCancelClaim?.()}>{t('common.cancel')}</Button>}
        </div>
      </SettingsPanel>
    );
  }
  if (reach.state === 'off' && reach.surface === 'browser') {
    const { problem } = reach;
    // An address that cannot start a claim has no button to press.
    const noAction = problem?.code === 'link_required';
    return (
      <SettingsPanel title={t('devices.reach.title')} description={t('devices.reach.lead')} actions={offBadge}>
        {!noAction && (
          <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center">
            <Button variant="brand" disabled={reach.busy} onClick={actions.onTurnOn}>
              {reach.busy && <LoaderCircle className="motion-safe:animate-spin" />}
              {t('devices.reach.turnOn')}
            </Button>
            <p className="text-[12px] leading-relaxed text-muted"><KeptTail text={t('devices.reach.firstTime')} /></p>
          </div>
        )}
        {problem && (
          <div role="alert" className="flex flex-col gap-2 border-t border-border px-5 py-3 md:flex-row md:items-center md:justify-between">
            <p className={`text-[12px] leading-relaxed ${noAction ? 'text-foreground' : 'text-destructive-ink'}`}>
              <KeptTail text={claimProblemText(t, problem)} />
            </p>
            {problem.code === 'claim_in_progress' && problem.claimId && (
              <Button variant="secondary" size="xs" className="shrink-0" onClick={() => actions.onCancelClaim?.(problem.claimId)}>
                {t('devices.claim.cancelOther')}
              </Button>
            )}
          </div>
        )}
        {/* An organization's Avibe is not bound this way: the one sentence that says how. */}
        <p className="border-t border-border px-5 py-2.5 text-[11px] leading-relaxed text-muted"><KeptTail text={t('devices.claim.problem.organization')} /></p>
      </SettingsPanel>
    );
  }
  if (reach.state === 'off') {
    return (
      <SettingsPanel title={t('devices.reach.title')} description={t('devices.reach.lead')} actions={offBadge}>
        {reach.account !== null ? (
          <>
            <ClaimFields account={reach.account} name={reach.name} nameState={reach.nameState} onNameChange={actions.onNameChange} />
            <div className="border-t border-border px-5 py-4">
              <Button variant="brand" disabled={reach.nameState === 'taken' || !reach.name} onClick={actions.onTurnOn}>
                {t('devices.reach.turnOn')}
              </Button>
            </div>
          </>
        ) : (
          <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center">
            <Button variant="brand" onClick={actions.onSignIn}>{t('devices.reach.signInAndTurnOn')}</Button>
            <p className="text-[12px] leading-relaxed text-muted">{t('devices.reach.signInHint')}</p>
          </div>
        )}
      </SettingsPanel>
    );
  }
  const on = reach.state === 'on';
  const down = on && reach.tunnel === 'down';
  const status = reach.state === 'paused'
    ? t('devices.reach.paused')
    : down
      ? t('devices.reach.down')
      : reach.tunnel === 'unknown'
        ? t('devices.reach.tunnelUnknown')
        : joinParts(t, [
            t(reach.tunnel === 'slow' ? 'devices.reach.slow'
              : reach.tunnel === 'unsteady' ? 'devices.reach.unsteady'
                : reach.tunnel === 'optimizing' ? 'devices.reach.optimizing' : 'devices.reach.good'),
            reach.ms ? t('remoteAccess.direct.legMs', { ms: reach.ms }) : null,
            reach.direct ? t('devices.reach.directOn') : null,
          ]);
  return (
    <SettingsPanel>
      {/* On a narrow screen the whole domain comes first; its buttons and the switch take the next line. */}
      <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center md:gap-3.5">
        <div className="flex min-w-0 flex-1 items-center gap-3.5">
          <IconTile tone={on && !down ? 'on' : 'plain'}><Globe size={18} /></IconTile>
          <div className="min-w-0 flex-1">
            <div className={`break-all font-mono text-[14px] font-semibold md:truncate ${reach.state === 'paused' ? 'text-muted' : 'text-foreground'}`}>{reach.host}</div>
            <div className="mt-0.5 flex min-w-0 flex-col gap-x-1.5 text-[11px] leading-relaxed text-muted md:flex-row md:items-center">
              {reach.account && <span className="truncate">{reach.account}</span>}
              {reach.account && <span aria-hidden className="max-md:hidden">{t('devices.dot')}</span>}
              <span className={down || (on && (reach.tunnel === 'slow' || reach.tunnel === 'unsteady')) ? 'text-gold-ink' : undefined}>{status}</span>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1.5 max-md:pl-[50px] md:shrink-0">
          {on && (
            <>
              <Button variant="ghost" size="2xs" aria-label={t('devices.reach.copyAddress')} onClick={actions.onCopyAddress}><Copy /></Button>
              <Button variant="ghost" size="2xs" aria-label={t('devices.reach.openAddress')} onClick={actions.onOpenAddress}><ExternalLink /></Button>
            </>
          )}
          {down && <Button variant="secondary" size="xs" onClick={actions.onRetry}><RefreshCw />{t('common.retry')}</Button>}
          <span className="ml-auto pl-2 text-[12px] text-muted md:ml-1">{t('devices.reach.title')}</span>
          <ToggleSwitch
            enabled={on}
            disabled={reach.busy}
            onClick={() => actions.onRemoteAccessChange?.(!on)}
            label={t('devices.reach.title')}
          />
        </div>
      </div>
      {operate ? on && (
        <div className="border-t border-border"><OptimizeRow optimize={optimize} onOptimize={actions.onOptimizeRoute} /></div>
      ) : (
        <button
          type="button"
          onClick={actions.onOpenConnection}
          className="flex items-center justify-between border-t border-border px-5 py-2.5 text-left text-[12px] text-muted transition-colors hover:bg-field/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <span>{t('devices.reach.connection')}</span>
          <ChevronRight className="size-4" />
        </button>
      )}
    </SettingsPanel>
  );
}

function functionDetail(t: TFunction, language: string, device: Device, fn: DeviceFunction): string {
  switch (fn.kind) {
    case 'notifications':
      return t(device.kind === 'host' ? 'devices.capability.notificationsHost' : 'devices.capability.notificationsPush');
    case 'health': {
      if (!fn.on) return t('devices.capability.healthOff');
      const data = upperFirst(healthText(t, language, fn.data));
      const synced = fn.syncedAt ? timeParts(fn.syncedAt, language) : null;
      return synced ? t(healthSyncedKeys[synced.day], { data, date: synced.date, time: synced.time }) : data;
    }
    case 'schedule':
      return t('devices.capability.scheduleDetail');
    case 'computerUse':
      return t(device.self ? 'devices.capability.computerUseHere' : 'devices.capability.computerUseThere');
  }
}

const computerUseKeys = {
  ready: 'devices.capability.computerUseReady',
  needs_permission: 'devices.capability.computerUseNeedsPermission',
  off: 'devices.capability.stateOff',
  unavailable: 'devices.capability.computerUseUnavailable',
} as const satisfies Record<Extract<DeviceFunction, { kind: 'computerUse' }>['status'], TranslationKey>;

const functionState = (t: TFunction, fn: DeviceFunction) => (
  fn.kind === 'computerUse' ? t(computerUseKeys[fn.status]) : t(fn.on ? 'devices.capability.stateOn' : 'devices.capability.stateOff')
);
const functionOn = (fn: DeviceFunction) => (fn.kind === 'computerUse' ? fn.status === 'ready' : fn.on);

function DeviceRow({ device, onOpen }: { device: Device; onOpen?: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  const Icon = deviceIcon[device.kind];
  const offered = device.functions.filter(functionOn).map((each) => t(functionKeys[each.kind]));
  // A device with no name of its own is already named by its kind: the line does not say it twice.
  const client = device.name === null && !device.client.version && !device.client.platform ? null : clientText(t, device.client);
  const second = joinParts(t, [device.person, client, device.pushOnly ? t('devices.list.pushOnly') : offered.join(t('devices.listSeparator'))]);
  // Where it is now: the path of a connection observed right now, or when it was last here. The
  // computer the page is open on says nothing: the page being open is the fact.
  const where = device.self && !device.online?.path ? null : device.online ? (
    <>
      <StatusDot tone="running" />
      <span className="text-foreground">{connectionText(t, device.online, false)}</span>
    </>
  ) : device.lastActiveAt ? <span>{deviceTime(device.lastActiveAt, t, i18n.language, 'ago')}</span> : null;
  return (
    <button
      type="button"
      onClick={() => onOpen?.(device.id)}
      className="flex w-full items-center gap-3.5 border-b border-border px-5 py-3 text-left transition-colors last:border-b-0 hover:bg-field/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
    >
      <IconTile><Icon size={18} /></IconTile>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="min-w-0 truncate text-[13px] font-medium text-foreground">{deviceName(t, device)}</span>
          {device.self && <Badge variant="info">{t('devices.list.thisDevice')}</Badge>}
          {device.kind === 'host' && <Badge variant="secondary">{t('devices.list.runsAvibe')}</Badge>}
        </div>
        <div className="mt-0.5 text-[11px] leading-relaxed text-muted md:truncate">{second}</div>
        {where && <div className="mt-1 flex items-center gap-1.5 text-[11px] text-muted md:hidden">{where}</div>}
      </div>
      {where && <div className="hidden shrink-0 items-center gap-1.5 text-[12px] text-muted md:flex">{where}</div>}
      <ChevronRight className="size-4 shrink-0 text-muted" />
    </button>
  );
}

function ActivityRow({ item, wide }: { item: DeviceActivity; wide?: boolean }) {
  const { t, i18n } = useTranslation();
  return (
    <div className="flex items-baseline gap-3 border-b border-border px-5 py-2.5 last:border-b-0">
      <span className={`shrink-0 text-[11px] tabular-nums text-muted ${wide ? 'w-28' : 'w-24'}`}>{deviceTime(item.at, t, i18n.language, 'stamp')}</span>
      <span className="min-w-0 flex-1 text-[12px] leading-relaxed text-foreground">{activityText(t, i18n.language, item)}</span>
      {/* The device, where the sentence itself does not already name it. */}
      {item.device && (item.event.kind === 'healthRead'
        || ((item.event.kind === 'alarmCreated' || item.event.kind === 'reminderCreated') && !item.event.confirmed)) && (
        <span className="hidden shrink-0 text-[11px] text-muted md:inline">{item.device}</span>
      )}
    </div>
  );
}

/** A device's detail: where it is, what it offers, what it did lately, one way out. */
function DeviceDetail({ device, account, member, actions }: {
  device: Device; account?: string; member: boolean; actions: DevicesPageActions;
}) {
  const { t, i18n } = useTranslation();
  const Icon = deviceIcon[device.kind];
  const facts: Array<[string, string]> = [];
  if (device.online) {
    facts.push([t('devices.detail.connection'), connectionText(t, device.online, true)]);
  } else if (device.lastActiveAt) {
    facts.push([t('devices.detail.lastActive'), deviceTime(device.lastActiveAt, t, i18n.language, 'ago')]);
  }
  if (device.signedInAt) facts.push([t('devices.detail.signedIn'), deviceTime(device.signedInAt, t, i18n.language, 'date')]);
  const wayOut = device.pushOnly
    ? t('devices.detail.pushOnly')
    : device.group === 'others'
      ? device.person ? t('devices.detail.signOutPerson', { person: device.person }) : t('devices.detail.signOutPersonPlain')
      : account
        ? t(member ? 'devices.detail.signOutMember' : 'devices.detail.signOutOwn', { account })
        : t('devices.detail.signOutPlain');
  return (
    <Dialog open onOpenChange={(open) => { if (!open) actions.onCloseDevice?.(); }}>
      <DialogContent className="max-w-md gap-0 p-0 max-md:p-0" closeLabel={t('common.close')}>
        <DialogHeader className="px-6 pb-4 pt-6 max-md:px-5 max-md:pt-3">
          <div className="flex items-center gap-3.5">
            <IconTile><Icon size={18} /></IconTile>
            <div className="min-w-0 text-left">
              <DialogTitle className="truncate text-[15px]">{deviceName(t, device)}</DialogTitle>
              <DialogDescription className="mt-0.5 truncate text-[12px]">
                {joinParts(t, [device.person, clientText(t, device.client)])}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>
        {facts.length > 0 && (
          <dl className="grid grid-cols-[72px_minmax(0,1fr)] gap-x-4 gap-y-1.5 border-t border-border px-6 py-3.5 text-[12px] max-md:px-5">
            {facts.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="text-muted">{label}</dt>
                <dd className="text-foreground">{value}</dd>
              </div>
            ))}
          </dl>
        )}
        {device.functions.length > 0 && (
          <div className="border-t border-border">
            <div className="px-6 pb-1 pt-3.5 text-[11px] font-semibold text-muted max-md:px-5">{t('devices.detail.functions')}</div>
            {device.functions.map((each) => {
              const FunctionIcon = functionIcon[each.kind];
              const name = t(functionKeys[each.kind]);
              return (
                <div key={each.kind} className="flex items-center gap-3 px-6 py-2.5 max-md:px-5">
                  <FunctionIcon className="size-4 shrink-0 text-muted" />
                  <div className="min-w-0 flex-1">
                    <div className="text-[13px] font-medium text-foreground">{name}</div>
                    <div className="text-[11px] leading-relaxed text-muted"><KeptTail text={functionDetail(t, i18n.language, device, each)} /></div>
                  </div>
                  {functionControl(each) === 'switch' && each.kind !== 'computerUse' ? (
                    <ToggleSwitch enabled={each.on} onClick={() => actions.onFunctionChange?.(device.id, each.kind, !each.on)} label={name} />
                  ) : (
                    <span className="shrink-0 text-right text-[12px] text-muted">{functionState(t, each)}</span>
                  )}
                </div>
              );
            })}
            {device.group === 'others' && (
              <p className="px-6 pb-1 pt-1 text-[11px] leading-relaxed text-muted max-md:px-5"><KeptTail text={t('devices.detail.othersFunctions')} /></p>
            )}
            <div className="h-2" />
          </div>
        )}
        {device.recent && device.recent.length > 0 && (
          <div className="border-t border-border pb-1">
            <div className="px-6 pb-1 pt-3.5 text-[11px] font-semibold text-muted max-md:px-5">{t('devices.detail.recent')}</div>
            {device.recent.map((item) => (
              <div key={item.id} className="flex items-baseline gap-3 px-6 py-1.5 max-md:px-5">
                <span className="w-24 shrink-0 text-[11px] tabular-nums text-muted">{deviceTime(item.at, t, i18n.language, 'stamp')}</span>
                <span className="min-w-0 flex-1 text-[12px] leading-relaxed text-foreground">{activityText(t, i18n.language, item)}</span>
              </div>
            ))}
          </div>
        )}
        {device.kind !== 'host' && (
          <div className="flex flex-col gap-2 border-t border-border px-6 py-4 max-md:px-5 md:flex-row md:items-center md:justify-between">
            <p className="text-[11px] leading-relaxed text-muted md:max-w-[250px]"><KeptTail text={wayOut} /></p>
            <Button variant="destructive-soft" size="sm" className="shrink-0" onClick={() => actions.onSignOutDevice?.(device.id)}>
              <LogOut />{t(device.pushOnly ? 'devices.detail.stopNotifications' : 'devices.detail.signOut')}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/**
 * 「添加设备」: where each client is really obtained today. The iPhone App is a TestFlight beta,
 * not in the App Store; there is no Android app, so an Android phone uses the web page from its
 * home screen; Avibe Desktop is downloaded from avibe.bot for Mac and Windows. One code at a time.
 */
function AddDevice({ kind, host, account, inline = false, actions }: {
  kind: AddKind; host: string; account?: string; inline?: boolean; actions: DevicesPageActions;
}) {
  const { t } = useTranslation();
  const signIn = (withAccount: TranslationKey, plain: TranslationKey) => (account ? t(withAccount, { account }) : t(plain));
  const steps: Record<AddKind, string[]> = {
    iphone: [t('devices.add.iphoneScan'), signIn('devices.add.iphoneSignIn', 'devices.add.iphoneSignInPlain'), t('devices.add.appears')],
    android: [t('devices.add.androidScan'), signIn('devices.add.androidSignIn', 'devices.add.androidSignInPlain'), t('devices.add.androidHomeScreen')],
    computer: [t('devices.add.computerInstall'), signIn('devices.add.computerSignIn', 'devices.add.computerSignInPlain'), t('devices.add.computerBrowser', { host })],
  };
  const note: Record<AddKind, string> = {
    iphone: t('devices.add.iphoneNote'),
    android: t('devices.add.androidNote'),
    computer: t('devices.add.computerNote'),
  };
  const code = kind === 'iphone' ? TESTFLIGHT_INVITE_URL : instanceUrl(host);
  const body = (
    <div className={inline ? 'px-5 pb-5 pt-1' : ''}>
      <SegmentedRadio
        ariaLabel={t('devices.add.which')}
        className="w-fit max-w-full"
        value={kind}
        onChange={actions.onAddKindChange ?? noop}
        options={[
          { id: 'iphone', label: t('devices.add.iphone') },
          { id: 'android', label: t('devices.add.android') },
          { id: 'computer', label: t('devices.add.computer') },
        ]}
      />
      <div className="mt-4 flex flex-col gap-5 md:flex-row md:items-start">
        {kind === 'computer' ? (
          <div className="flex shrink-0 flex-col gap-2 md:w-[132px]">
            <Button variant="secondary" size="sm" onClick={actions.onOpenDownloads}><Download />{t('devices.add.downloads')}</Button>
            <span className="text-center font-mono text-[10px] text-muted">{t('devices.add.downloadsFrom')}</span>
          </div>
        ) : (
          <div className="flex shrink-0 flex-col gap-1.5 md:w-[134px]">
            {/* The code stays dark on light in both themes, so every camera reads it. */}
            <div className="self-start rounded-lg border border-edge bg-white p-2.5" data-testid="add-device-code" data-code={code}>
              <QRCodeSVG value={code} size={112} />
            </div>
            {/* A phone cannot scan its own screen: the same address, to copy. */}
            <div className="flex min-w-0 items-center gap-1">
              <span className="min-w-0 truncate font-mono text-[10px] text-muted" title={code}>{code}</span>
              <Button variant="ghost" size="2xs" aria-label={t('devices.add.copyLink')} onClick={() => actions.onCopyLink?.(code)}><Copy /></Button>
            </div>
          </div>
        )}
        <div className="min-w-0 flex-1">
          <ol className="flex list-decimal flex-col gap-1.5 pl-4 text-[13px] leading-relaxed text-foreground">
            {steps[kind].map((step) => <li key={step}>{step}</li>)}
          </ol>
          <p className="mt-2.5 text-[11px] leading-relaxed text-muted"><KeptTail text={note[kind]} /></p>
        </div>
      </div>
    </div>
  );
  if (inline) return body;
  return (
    <Dialog open onOpenChange={(open) => { if (!open) actions.onCloseAdd?.(); }}>
      <DialogContent className="max-w-lg" closeLabel={t('common.close')}>
        <DialogHeader>
          <DialogTitle>{t('devices.add.title')}</DialogTitle>
        </DialogHeader>
        {body}
      </DialogContent>
    </Dialog>
  );
}

/** Health connections no listed device can be proven to own: one row each, disconnected on its own. */
function UnlinkedHealthPanel({ items, onDisconnect }: { items: UnlinkedHealth[]; onDisconnect?: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  return (
    <SettingsPanel title={t('devices.unlinkedHealth.title')} description={<KeptTail text={t('devices.unlinkedHealth.description')} />}>
      {items.map((item) => (
        <div key={item.id} className="flex items-center gap-3.5 border-b border-border px-5 py-3 last:border-b-0">
          <IconTile><HeartPulse size={18} /></IconTile>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[13px] font-medium text-foreground">{item.device || t('devices.kindName.app')}</div>
            <div className="text-[11px] leading-relaxed text-muted">
              {joinParts(t, [
                item.data.length > 0 ? upperFirst(healthText(t, i18n.language, item.data)) : null,
                item.lastSeenAt ? t('devices.unlinkedHealth.lastSeen', { when: deviceTime(item.lastSeenAt, t, i18n.language, 'date') }) : null,
              ])}
            </div>
          </div>
          <Button variant="destructive-soft" size="sm" className="shrink-0" onClick={() => onDisconnect?.(item.id)}>{t('devices.unlinkedHealth.disconnect')}</Button>
        </div>
      ))}
    </SettingsPanel>
  );
}

const accountIcon = {
  queued: Clock, retrying: LoaderCircle, accepted: Check, localOnly: LogOut, sharedSignIn: LogOut, keeperUnknown: LogOut,
  refusedCredential: X, refused: X, unconfirmed: Clock, unknown: Clock,
} as const satisfies Record<AccountResult, unknown>;
const accountTone = (result: AccountResult) => (
  result === 'accepted' ? 'text-mint-ink'
    : result === 'refused' || result === 'refusedCredential' || result === 'unconfirmed' ? 'text-gold-ink' : 'text-muted'
);
/** The result that most needs the person's eye leads the row's icon. */
const leading = (results: AccountResult[]): AccountResult => (
  (['refusedCredential', 'refused', 'unconfirmed', 'localOnly', 'keeperUnknown', 'sharedSignIn', 'retrying', 'queued', 'unknown', 'accepted'] as const)
    .find((each) => results.includes(each)) ?? 'unknown'
);

/** Devices signed out lately. Each is out of this Avibe; the lines say what avibe.bot did about
 *  the account sign-in, and never call an accepted request an ended sign-in. */
function SignedOutPanel({ rows }: { rows: SignedOutDevice[] }) {
  const { t } = useTranslation();
  return (
    <SettingsPanel title={t('devices.signedOut.title')}>
      {rows.map((row) => {
        const lead = leading(row.results);
        const Icon = accountIcon[lead];
        return (
          <div key={row.id} className="flex items-start gap-3 border-b border-border px-5 py-3 last:border-b-0">
            <Icon className={`mt-0.5 size-4 shrink-0 ${accountTone(lead)}`} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className="text-[13px] font-medium text-foreground">{row.device ?? t('devices.signedOut.unnamed')}</span>
                <span className="text-[11px] text-muted">{t('devices.signedOut.here')}</span>
              </div>
              {accountResultTexts(t, row).map((line) => (
                <div key={line} className="mt-0.5 text-[12px] leading-relaxed text-foreground"><KeptTail text={line} /></div>
              ))}
            </div>
          </div>
        );
      })}
    </SettingsPanel>
  );
}

/** Right after a sign-out: what avibe.bot was asked or not asked, and Health only here, because
 *  only this answer says anything about it. */
function SignOutNoticeCard({ notice, onDismiss }: { notice: SignOutNotice; onDismiss?: () => void }) {
  const { t } = useTranslation();
  return (
    <Notice tone="plain" action={<Button variant="ghost" size="xs" onClick={onDismiss}>{t('common.close')}</Button>}>
      <div className="font-medium">
        {notice.device ? t('devices.signedOut.justNow', { device: notice.device }) : t('devices.signedOut.justNowMany')}
      </div>
      {accountResultTexts(t, notice).map((line) => <div key={line}><KeptTail text={line} /></div>)}
      {notice.healthCleanupPending && (
        <div className="text-muted"><KeptTail text={t(notice.hadHealth ? 'devices.signedOut.healthPending' : 'devices.signedOut.healthPendingIfAny')} /></div>
      )}
    </Notice>
  );
}

export function DevicesPageView({
  reach, devices, list, activity = [], moreActivity, viewer = 'owner', operate = false, optimize, viewerAccount, bulk = null, unlinkedHealth = [], signedOut = [],
  signOutNotice, unrecognizedChange, problem, detail, confirm, busy, view = 'page', add, ...actions
}: DevicesPageViewProps) {
  const { t } = useTranslation();
  // Each device says which list it belongs to; nothing is inferred from an absent account.
  const mine = devices.filter((each) => each.group === 'mine');
  const others = devices.filter((each) => each.group === 'others');
  const unattributed = devices.filter((each) => each.group === 'unattributed');
  const opened = devices.find((each) => each.id === detail);
  const owner = viewer === 'owner';
  const account = viewerAccount;
  const on = reach.state === 'on';
  const signedIn = othersSignedIn(devices);
  const remote = remoteDevices(devices);
  const latest = newestFirst(activity);

  if (view === 'activity') {
    return (
      <SettingsPageShell title={t('devices.activity.title')} subtitle={t('devices.activity.subtitle')} activeTab="remote-access">
        <BackToDevices onBack={actions.onBack} />
        <SettingsPanel>
          {latest.length > 0
            ? latest.map((item) => <ActivityRow key={item.id} item={item} wide />)
            : <p className="px-5 py-3 text-[12px] text-muted">{t('devices.activity.empty')}</p>}
          {moreActivity && (
            <div className="flex justify-center border-t border-border px-5 py-2.5">
              <Button variant="ghost" size="compact" disabled={moreActivity === 'loading'} onClick={actions.onMoreActivity}>
                {moreActivity === 'loading' && <LoaderCircle className="motion-safe:animate-spin" />}
                {t('devices.activity.earlier')}
              </Button>
            </div>
          )}
        </SettingsPanel>
      </SettingsPageShell>
    );
  }

  if (reach.state === 'moved') {
    return (
      <SettingsPageShell title={t('devices.title')} subtitle={t('devices.subtitle')} activeTab="remote-access">
        <ReachCard reach={reach} actions={actions} />
      </SettingsPageShell>
    );
  }

  const self = devices.find((each) => each.self);
  const host = devices.find((each) => each.kind === 'host');
  const selfName = self && self.kind !== 'host' ? deviceName(t, self) : null;
  const atHost = self?.kind === 'host';
  const othersSyncHealth = (bulk === 'all_remote' ? remote : signedIn).some(signOutNeedsConfirm);
  // Until a device besides the computer that runs Avibe is listed, the way to add one is in the list.
  const onlyHost = mine.every((each) => each.kind === 'host');
  const locked = lockedOutCount(devices);
  // A member sees only their own devices, so how many others are locked out is not theirs to count.
  const lockedText = !owner ? t('devices.confirm.turnOffLockedAll') : locked > 0
    ? host?.name
      ? t('devices.confirm.turnOffLocked', { count: locked, host: host.name })
      : t('devices.confirm.turnOffLockedHost', { count: locked })
    : host?.name
      ? t('devices.confirm.turnOffLockedAlone', { host: host.name })
      : t('devices.confirm.turnOffLockedAloneHost');
  // The domain is the owner's: only they are told it is kept for them.
  const kept = t(owner ? 'devices.confirm.turnOffKept' : 'devices.confirm.turnOffKeptMember');
  // Who is turning it off is said only when this device is known, and by its name.
  const turnOffBody = selfName
    ? t('devices.confirm.turnOffBodyFrom', { from: t('devices.confirm.turnOffFrom', { device: selfName }), locked: lockedText, kept })
    : t('devices.confirm.turnOffBodyPlain', { locked: lockedText, kept });
  const unlinkedOpen = typeof confirm === 'object' ? unlinkedHealth.find((each) => each.id === confirm.unlinkedHealth) : undefined;
  const confirmProps = { busy, onConfirm: actions.onConfirm, onCancel: actions.onCancelConfirm };
  let bulkRow: ReactNode = null;
  if (bulk === 'other_sessions' && signedIn.length > 1) {
    bulkRow = <Button variant="ghost" size="compact" className="text-destructive-ink" onClick={actions.onSignOutOthers}>{t('devices.list.signOutOthers')}</Button>;
  } else if (bulk === 'all_remote' && remote.length > 1) {
    bulkRow = <Button variant="ghost" size="compact" className="text-destructive-ink" onClick={actions.onSignOutOthers}>{t('devices.list.signOutAllRemote')}</Button>;
  }

  return (
    <SettingsPageShell
      title={t('devices.title')}
      subtitle={owner ? t('devices.subtitle') : t('devices.subtitleMemberPlain')}
      activeTab="remote-access"
    >
      {owner && unrecognizedChange && (
        <Notice tone="danger" action={<Button variant="destructive" size="sm" onClick={actions.onOpenRebind}>{t('devices.outsideChange.action')}</Button>}>
          <div className="text-balance text-[14px] font-semibold">{t('devices.outsideChange.title')}</div>
          <p className="mt-1"><KeptTail text={t('devices.outsideChange.body', { host: unrecognizedChange.hostname })} /></p>
          <p className="mt-1"><KeptTail text={t('devices.rebind.copiedData')} /></p>
        </Notice>
      )}
      {/* After turning remote access off from afar, not reading its state is the expected result:
          the notice below says so, and the card does not add "cannot be read" to it. */}
      {owner && !(problem === 'offFromAfar' && reach.state === 'unknown') && <ReachCard reach={reach} actions={actions} />}
      {!owner && operate && (reach.state === 'on' || reach.state === 'paused') && <ReachCard reach={reach} operate optimize={optimize} actions={actions} />}
      {signOutNotice && <SignOutNoticeCard notice={signOutNotice} onDismiss={actions.onDismissNotice} />}
      {problem && (
        <Notice action={<Button variant="ghost" size="xs" onClick={actions.onDismissNotice}>{t('common.close')}</Button>}>
          <KeptTail text={t(problemKeys[problem])} />
        </Notice>
      )}

      <SettingsPanel
        title={t('devices.list.mine')}
        actions={on && !onlyHost
          ? <Button variant="text" size="compact" onClick={actions.onAddDevice}><Plus />{t('devices.list.add')}</Button>
          : undefined}
      >
        {mine.map((each) => <DeviceRow key={each.id} device={each} onOpen={actions.onOpenDevice} />)}
        {mine.length === 0 && (list === 'unknown' ? (
          <div role="alert" className="flex flex-col gap-3 px-5 py-3 md:flex-row md:items-center md:justify-between">
            <p className="text-[12px] leading-relaxed text-foreground">{t('devices.list.unknown')}</p>
            <Button variant="secondary" size="xs" onClick={actions.onRetry}><RefreshCw />{t('common.retry')}</Button>
          </div>
        ) : (
          <p className="px-5 py-3 text-[12px] text-muted">{t(list === 'loading' ? 'common.loading' : 'devices.list.empty')}</p>
        ))}
        {owner && (reach.state === 'off' || reach.state === 'paused') && mine.length === 1 && onlyHost && (
          <p className="px-5 py-3 text-[12px] text-muted">{t('devices.list.emptyOff')}</p>
        )}
        {on && onlyHost && (
          <div className={mine.length > 0 ? 'border-t border-border' : undefined}>
            <div className="px-5 pb-2 pt-4 text-[13px] font-semibold text-foreground">{t('devices.list.add')}</div>
            <AddDevice inline kind={add ?? 'iphone'} host={reach.host} account={account} actions={actions} />
          </div>
        )}
        {bulkRow && (
          <div className="flex items-center justify-between gap-3 px-5 py-2.5">
            <span className="text-[11px] text-muted">{t('devices.list.unrecognised')}</span>
            {bulkRow}
          </div>
        )}
      </SettingsPanel>

      {owner && others.length > 0 && (
        <SettingsPanel title={t('devices.list.others')} description={t('devices.list.othersHint')}>
          {others.map((each) => <DeviceRow key={each.id} device={each} onOpen={actions.onOpenDevice} />)}
        </SettingsPanel>
      )}

      {owner && unattributed.length > 0 && (
        <SettingsPanel title={t('devices.list.unattributed')} description={<KeptTail text={t('devices.list.unattributedHint')} />}>
          {unattributed.map((each) => <DeviceRow key={each.id} device={each} onOpen={actions.onOpenDevice} />)}
        </SettingsPanel>
      )}

      {unlinkedHealth.length > 0 && <UnlinkedHealthPanel items={unlinkedHealth} onDisconnect={actions.onDisconnectHealth} />}
      {signedOut.length > 0 && <SignedOutPanel rows={signedOut} />}

      {owner && latest.length > 0 && (
        <SettingsPanel
          title={t('devices.list.recentActivity')}
          actions={<Button variant="text" size="compact" onClick={actions.onShowActivity}>{t('devices.list.all')}<ChevronRight /></Button>}
        >
          {latest.slice(0, RECENT_ACTIVITY_LIMIT).map((item) => <ActivityRow key={item.id} item={item} />)}
        </SettingsPanel>
      )}

      {add && on && !onlyHost && <AddDevice kind={add} host={reach.host} account={account} actions={actions} />}
      {opened && !confirm && <DeviceDetail device={opened} account={account} member={!owner} actions={actions} />}
      {opened && confirm === 'remove' && (
        <Confirm
          title={t('devices.confirm.removeTitle', { name: deviceName(t, opened) })}
          // At the computer itself the data goes only once the owner is confirmed.
          body={t(atHost ? 'devices.confirm.removeBodyAtHost' : 'devices.confirm.removeBody')}
          action={t(atHost ? 'devices.confirm.removeActionAtHost' : 'devices.confirm.removeAction')}
          {...confirmProps}
        />
      )}
      {(opened && confirm === 'health-off' || unlinkedOpen) && (
        <Confirm title={t('devices.confirm.healthOffTitle')} body={t('devices.confirm.healthOffBody')} action={t('devices.confirm.healthOffAction')} {...confirmProps} />
      )}
      {confirm === 'turn-off' && (
        <Confirm title={t('devices.confirm.turnOffTitle')} body={turnOffBody} action={t('devices.confirm.turnOffAction')} {...confirmProps} />
      )}
      {confirm === 'remove-others' && (
        <Confirm
          title={t('devices.confirm.removeOthersTitle')}
          body={account
            ? t(othersSyncHealth ? 'devices.confirm.removeOthersBodyHealth' : 'devices.confirm.removeOthersBody', { count: signedIn.length, account })
            : t(othersSyncHealth ? 'devices.confirm.removeOthersBodyHealthPlain' : 'devices.confirm.removeOthersBodyPlain', { count: signedIn.length })}
          action={t('devices.confirm.removeOthersAction')}
          {...confirmProps}
        />
      )}
      {confirm === 'remove-all-remote' && (
        <Confirm
          title={t('devices.confirm.removeAllRemoteTitle')}
          body={t(othersSyncHealth ? 'devices.confirm.removeAllRemoteBodyHealth' : 'devices.confirm.removeAllRemoteBody')}
          action={t('devices.confirm.removeOthersAction')}
          {...confirmProps}
        />
      )}
    </SettingsPageShell>
  );
}
