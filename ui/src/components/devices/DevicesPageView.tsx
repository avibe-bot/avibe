// Settings · Devices (ui/design: settings/devices/*): the one page for remote access, the devices
// that reach this Avibe, what each offers, and recent activity. Three layers: how this Avibe is
// reached (set once), the devices (one row each, a detail with its functions and one way out), and
// recent activity. No pairing key anywhere: remote access uses the sign-in this device already has.
// A view: props only. The container reads the APIs, maps them onto `./types`, and passes handlers.
import type { ReactNode } from 'react';
import {
  AlarmClock, ArrowLeft, Bell, ChevronRight, Copy, Download, ExternalLink, Globe, HeartPulse, Laptop, LogOut, Monitor,
  MonitorCog, Plus, RefreshCw, Smartphone,
} from 'lucide-react';
import { QRCodeSVG } from 'qrcode.react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import type { TranslationKey } from '@/i18n/types';
import { accountInitialFor } from '../AccountAvatar';
import { SettingsPageShell } from '../settings/SettingsPageShell';
import { SettingsPanel, ToggleSwitch } from '../settings/SettingsPrimitives';
import { StatusDot } from '../SidebarStatusRow';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog';
import { Input } from '../ui/input';
import { SegmentedRadio } from '../ui/segmented';
import { clientText, connectionText, deviceTime, healthText, joinParts, timeParts, upperFirst } from './format';
import {
  DOMAIN_SUFFIX, RECENT_ACTIVITY_LIMIT, instanceUrl, TESTFLIGHT_INVITE_URL, activityText, capabilityControl, lockedOutCount, newestFirst,
  othersSignedIn, signOutNeedsConfirm,
} from './rules';
import type {
  AddKind, CapabilityKind, Device, DeviceActivity, DeviceCapability, DevicesConfirm, NameState, Reach,
} from './types';


const capabilityKeys = {
  notifications: 'devices.capability.notifications',
  health: 'devices.capability.health',
  schedule: 'devices.capability.schedule',
  computerUse: 'devices.capability.computerUse',
} as const satisfies Record<CapabilityKind, TranslationKey>;
const capabilityIcon = { notifications: Bell, health: HeartPulse, schedule: AlarmClock, computerUse: MonitorCog } as const;
const deviceIcon = { host: Laptop, iphone: Smartphone, desktop: Monitor, browser: Globe } as const;


export type DevicesPageActions = {
  onTurnOn?: () => void;
  onSignIn?: () => void;
  onNameChange?: (name: string) => void;
  onSignInAs?: (account: string) => void;
  onRemoteAccessChange?: (on: boolean) => void;
  onRetry?: () => void;
  onCopyAddress?: () => void;
  onOpenAddress?: () => void;
  onOpenConnection?: () => void;
  onOpenDevice?: (id: string) => void;
  onCloseDevice?: () => void;
  onCapabilityChange?: (deviceId: string, kind: CapabilityKind, on: boolean) => void;
  onSignOutDevice?: (id: string) => void;
  onSignOutOthers?: () => void;
  onConfirm?: () => void;
  onCancelConfirm?: () => void;
  onShowActivity?: () => void;
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
  /** A member of a shared instance sees only their own devices and no instance controls. */
  viewer?: 'owner' | 'member';
  /** The account this Avibe belongs to. */
  ownerEmail?: string;
  /** The account the person looking is signed in as; the owner's unless `viewer` is a member. */
  viewerAccount?: string;
  /** The device whose detail is open. */
  detail?: string;
  /** A way out was pressed and something would be lost: the confirmation. */
  confirm?: DevicesConfirm;
  /** The full activity list in place of the page. */
  view?: 'page' | 'activity';
  /** 「添加设备」 on one of its tabs: inline while this computer is the only device, else a dialog. */
  add?: AddKind;
  /** Owner-only content under how this Avibe is reached (a row a later change adds there). */
  afterReach?: ReactNode;
  /** Content under the device lists, above recent activity (a panel a later change adds there). */
  afterDevices?: ReactNode;
};

const noop = () => undefined;

const healthSyncedKeys = {
  today: 'devices.capability.healthSyncedToday',
  yesterday: 'devices.capability.healthSyncedYesterday',
  other: 'devices.capability.healthSyncedDated',
} as const satisfies Record<NonNullable<ReturnType<typeof timeParts>>['day'], TranslationKey>;

/** Back to the Devices page, as other Settings sub-pages draw it; shown at every width. */
export function BackToDevices({ onBack }: { onBack?: () => void }) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onBack}
      className="inline-flex w-fit items-center gap-1.5 font-mono text-[11px] text-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <ArrowLeft className="size-3" />
      {t('devices.backToDevices')}
    </button>
  );
}

/** The account, drawn as the App's own account row draws it: the initial and the email. */
function Account({ email, note }: { email: string; note?: string }) {
  return (
    <span className="flex min-w-0 items-center gap-2">
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-mint-soft text-[11px] font-semibold text-mint-ink">
        {accountInitialFor(email)}
      </span>
      <span className="min-w-0 truncate text-[13px] font-medium text-foreground">{email}</span>
      {note && <span className="shrink-0 text-[11px] text-muted">{note}</span>}
    </span>
  );
}

function IconTile({ children, tone = 'plain' }: { children: ReactNode; tone?: 'plain' | 'on' }) {
  return (
    <span className={`flex size-9 shrink-0 items-center justify-center rounded-[10px] ${tone === 'on' ? 'bg-mint-soft text-mint-ink' : 'bg-field text-muted'}`}>
      {children}
    </span>
  );
}

/**
 * The claim: the account this App is signed in to, and the name that becomes the domain. Every new
 * binding passes through it; no domain is ever assigned at random. The field is prefilled and always
 * editable (owner, 2026-11-20): the part of the account's email before the @, cleaned to lowercase
 * letters, digits and dashes. When this account's own first Avibe already holds that prefix, the
 * prefill is the prefix plus this computer's name (`max-macmini`). When someone else holds the name
 * it says 「已被占用」 and the person changes it; a number is never appended for them. The same rule
 * on every path: this page, Setup, the avibe.bot screen, the terminal link.
 */
export function ClaimFields({ account, name, nameState, onNameChange }: {
  account: string;
  name: string;
  nameState?: NameState;
  onNameChange?: (name: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="grid gap-x-6 gap-y-2 px-5 py-4 md:grid-cols-[72px_minmax(0,1fr)] md:items-center md:gap-y-3">
      <span className="text-[12px] text-muted">{t('devices.claim.account')}</span>
      <Account email={account} note={t('devices.claim.signedInHere')} />
      <span className="text-[12px] text-muted max-md:mt-2">{t('devices.claim.name')}</span>
      <div className="flex min-w-0 items-center gap-2">
        <Input
          className="w-44 text-[13px]"
          value={name}
          readOnly={!onNameChange}
          onChange={onNameChange ? (event) => onNameChange(event.target.value) : undefined}
          aria-label={t('devices.claim.name')}
          aria-invalid={nameState === 'taken' || undefined}
        />
        <span className="truncate font-mono text-[12px] text-muted">{t('devices.claim.suffix')}</span>
      </div>
      <span className="max-md:hidden" />
      {nameState === 'taken' ? (
        <span className="text-[11px] text-destructive-ink md:-mt-1.5">{t('devices.claim.taken')}</span>
      ) : (
        <span className="text-[11px] text-muted md:-mt-1.5">{t('devices.claim.domain', { host: `${name}${DOMAIN_SUFFIX}` })}</span>
      )}
    </div>
  );
}

/** Layer 1: how this Avibe is reached. Set once, so it is compact once it is on. */
function ReachCard({ reach, actions }: { reach: Reach; actions: DevicesPageActions }) {
  const { t } = useTranslation();
  const offBadge = <Badge variant="secondary">{t('devices.reach.off')}</Badge>;
  if (reach.state === 'off' && reach.surface === 'browser') {
    return (
      <SettingsPanel title={t('devices.reach.title')} description={t('devices.reach.lead')} actions={offBadge}>
        <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center">
          <Button variant="brand" onClick={actions.onTurnOn}>{t('devices.reach.turnOn')}</Button>
          <p className="text-[12px] leading-relaxed text-muted">{t('devices.reach.firstTime')}</p>
        </div>
        {reach.error === 'unreachable' && (
          <p role="alert" className="border-t border-border px-5 py-3 text-[12px] text-destructive-ink">{t('devices.reach.unreachable')}</p>
        )}
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
  if (reach.state === 'conflict') {
    return (
      <SettingsPanel
        title={t('devices.reach.conflictTitle')}
        description={t('devices.reach.conflictBody', { host: reach.host, owner: reach.owner })}
        actions={<Badge variant="warning">{t('devices.reach.off')}</Badge>}
      >
        <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center">
          <Button variant="secondary" onClick={() => actions.onSignInAs?.(reach.owner)}>
            {t('devices.reach.signInAs', { owner: reach.owner })}
          </Button>
          <p className="text-[12px] leading-relaxed text-muted">{t('devices.reach.confirmedAs', { account: reach.account })}</p>
        </div>
      </SettingsPanel>
    );
  }
  const on = reach.state === 'on';
  const starting = reach.state === 'starting';
  const down = on && reach.tunnel === 'down';
  const status = starting
    ? t('devices.reach.starting')
    : reach.state === 'paused'
      ? t('devices.reach.paused')
      : down
        ? t('devices.reach.down')
        : joinParts(t, [
            t('devices.reach.good'),
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
              <span className="truncate">{reach.account}</span>
              <span aria-hidden className="max-md:hidden">{t('devices.dot')}</span>
              <span className={down ? 'text-gold-ink' : undefined}>{status}</span>
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
            enabled={on || starting}
            disabled={starting}
            onClick={() => actions.onRemoteAccessChange?.(!on)}
            label={t('devices.reach.title')}
          />
        </div>
      </div>
      {on && (
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

function capabilityDetail(t: TFunction, language: string, device: Device, capability: DeviceCapability): string {
  switch (capability.kind) {
    case 'notifications':
      return t(device.kind === 'host' ? 'devices.capability.notificationsHost' : 'devices.capability.notificationsPush');
    case 'health': {
      const data = upperFirst(healthText(t, language, capability.data));
      const synced = capability.syncedAt ? timeParts(capability.syncedAt, language) : null;
      return synced ? t(healthSyncedKeys[synced.day], { data, date: synced.date, time: synced.time }) : data;
    }
    case 'schedule':
      return t('devices.capability.scheduleDetail');
    case 'computerUse':
      return capability.fixed
        ? t(capability.on ? 'devices.capability.computerUseFixedOn' : 'devices.capability.computerUseFixedOff')
        : t('devices.capability.computerUseHere');
  }
}


function DeviceRow({ device, onOpen }: { device: Device; onOpen?: (id: string) => void }) {
  const { t, i18n } = useTranslation();
  const Icon = deviceIcon[device.kind];
  const offered = device.capabilities.filter((each) => each.on).map((each) => t(capabilityKeys[each.kind]));
  const second = joinParts(t, [device.person, clientText(t, device.client), offered.join(t('devices.listSeparator'))]);
  // Where it is now: the path its connection uses, or when it was last here. The computer the page
  // is open on says nothing: the page being open is the fact.
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
          <span className="min-w-0 truncate text-[13px] font-medium text-foreground">{device.name}</span>
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
      {item.source && <span className="hidden shrink-0 text-[11px] text-muted md:inline">{item.source}</span>}
    </div>
  );
}

/** A device's detail: where it is, what it offers (a switch each), what it did lately, one way out. */
function DeviceDetail({ device, account, member, actions }: {
  device: Device; account: string; member: boolean; actions: DevicesPageActions;
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
  return (
    <Dialog open onOpenChange={(open) => { if (!open) actions.onCloseDevice?.(); }}>
      <DialogContent className="max-w-[480px] gap-0 p-0 max-md:p-0" closeLabel={t('common.close')}>
        <DialogHeader className="px-6 pb-4 pt-6 max-md:px-5 max-md:pt-3">
          <div className="flex items-center gap-3.5">
            <IconTile><Icon size={18} /></IconTile>
            <div className="min-w-0 text-left">
              <DialogTitle className="truncate text-[15px]">{device.name}</DialogTitle>
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
        {device.capabilities.length > 0 && (
          <div className="border-t border-border">
            <div className="px-6 pb-1 pt-3.5 text-[11px] font-semibold text-muted max-md:px-5">{t('devices.detail.functions')}</div>
            {device.capabilities.map((each) => {
              const CapabilityIcon = capabilityIcon[each.kind];
              const name = t(capabilityKeys[each.kind]);
              return (
                <div key={each.kind} className="flex items-center gap-3 px-6 py-2.5 max-md:px-5">
                  <CapabilityIcon className="size-4 shrink-0 text-muted" />
                  <div className="min-w-0 flex-1">
                    <div className="text-[13px] font-medium text-foreground">{name}</div>
                    <div className="text-[11px] leading-relaxed text-muted">{capabilityDetail(t, i18n.language, device, each)}</div>
                  </div>
                  {capabilityControl(device, each) === 'switch' ? (
                    <ToggleSwitch enabled={each.on} onClick={() => actions.onCapabilityChange?.(device.id, each.kind, !each.on)} label={name} />
                  ) : (
                    <span className="shrink-0 text-[12px] text-muted">{t(each.on ? 'devices.capability.stateOn' : 'devices.capability.stateOff')}</span>
                  )}
                </div>
              );
            })}
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
            <p className="text-[11px] leading-relaxed text-muted md:max-w-[250px]">
              {device.group === 'others'
                ? t('devices.detail.signOutPerson', { person: device.person ?? '' })
                : t(member ? 'devices.detail.signOutMember' : 'devices.detail.signOutOwn', { account })}
            </p>
            <Button variant="destructive-soft" size="sm" className="shrink-0" onClick={() => actions.onSignOutDevice?.(device.id)}>
              <LogOut />{t('devices.detail.signOut')}
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
  kind: AddKind; host: string; account: string; inline?: boolean; actions: DevicesPageActions;
}) {
  const { t } = useTranslation();
  const steps: Record<AddKind, string[]> = {
    iphone: [t('devices.add.iphoneScan'), t('devices.add.iphoneSignIn', { account }), t('devices.add.appears')],
    android: [t('devices.add.androidScan'), t('devices.add.androidSignIn', { account }), t('devices.add.androidHomeScreen')],
    computer: [t('devices.add.computerInstall'), t('devices.add.computerSignIn', { account }), t('devices.add.computerBrowser', { host })],
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
          // The code stays dark on light in both themes, so every camera reads it.
          <div className="flex shrink-0 flex-col gap-1.5 md:w-[134px]">
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
          <p className="mt-2.5 text-[11px] leading-relaxed text-muted">{note[kind]}</p>
        </div>
      </div>
    </div>
  );
  if (inline) return body;
  return (
    <Dialog open onOpenChange={(open) => { if (!open) actions.onCloseAdd?.(); }}>
      <DialogContent className="max-w-[520px]" closeLabel={t('common.close')}>
        <DialogHeader>
          <DialogTitle>{t('devices.add.title')}</DialogTitle>
        </DialogHeader>
        {body}
      </DialogContent>
    </Dialog>
  );
}

function Confirm({ title, body, action, actions }: { title: string; body: string; action: string; actions: DevicesPageActions }) {
  const { t } = useTranslation();
  return (
    <Dialog open onOpenChange={(open) => { if (!open) actions.onCancelConfirm?.(); }}>
      <DialogContent className="max-w-md" closeLabel={t('common.close')}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{body}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={actions.onCancelConfirm}>{t('common.cancel')}</Button>
          <Button variant="destructive" onClick={actions.onConfirm}>{action}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function DevicesPageView({
  reach, devices, activity = [], viewer = 'owner', ownerEmail, viewerAccount, detail, confirm, view = 'page', add, afterReach, afterDevices, ...actions
}: DevicesPageViewProps) {
  const { t } = useTranslation();
  // Each device says which list it belongs to; push rows whose origin cannot be shown
  // (`unattributed`) are counted nowhere here.
  const mine = devices.filter((each) => each.group === 'mine');
  const others = devices.filter((each) => each.group === 'others');
  const opened = devices.find((each) => each.id === detail);
  const owner = viewer === 'owner';
  const account = viewerAccount ?? (owner ? ownerEmail : undefined) ?? '';
  const on = reach.state === 'on';
  const signedIn = othersSignedIn(devices);
  const latest = newestFirst(activity);

  if (view === 'activity') {
    return (
      <SettingsPageShell title={t('devices.activity.title')} subtitle={t('devices.activity.subtitle')} activeTab="remote-access">
        <BackToDevices onBack={actions.onBack} />
        <SettingsPanel>
          {latest.length > 0
            ? latest.map((item) => <ActivityRow key={item.id} item={item} wide />)
            : <p className="px-5 py-3 text-[12px] text-muted">{t('devices.activity.empty')}</p>}
        </SettingsPanel>
      </SettingsPageShell>
    );
  }

  const self = devices.find((each) => each.self);
  const host = devices.find((each) => each.kind === 'host');
  const othersSyncHealth = signedIn.some(signOutNeedsConfirm);
  // Until a device besides the computer that runs Avibe is listed, the way to add one is in the list.
  const onlyHost = mine.every((each) => each.kind === 'host');
  const locked = lockedOutCount(devices);
  const lockedText = locked > 0
    ? host?.name
      ? t('devices.confirm.turnOffLocked', { count: locked, host: host.name })
      : t('devices.confirm.turnOffLockedHost', { count: locked })
    : host?.name
      ? t('devices.confirm.turnOffLockedAlone', { host: host.name })
      : t('devices.confirm.turnOffLockedAloneHost');
  // Who is turning it off is said only when this device is known, and by its name.
  const turnOffBody = self && self.kind !== 'host' && self.name
    ? t('devices.confirm.turnOffBodyFrom', { from: t('devices.confirm.turnOffFrom', { device: self.name }), locked: lockedText, kept: t('devices.confirm.turnOffKept') })
    : t('devices.confirm.turnOffBodyPlain', { locked: lockedText, kept: t('devices.confirm.turnOffKept') });

  return (
    <SettingsPageShell
      title={t('devices.title')}
      subtitle={owner ? t('devices.subtitle') : t('devices.subtitleMember', { owner: ownerEmail ?? '' })}
      activeTab="remote-access"
    >
      {owner && <ReachCard reach={reach} actions={actions} />}
      {owner && afterReach}

      <SettingsPanel
        title={t('devices.list.mine')}
        actions={on && !onlyHost
          ? <Button variant="text" size="compact" onClick={actions.onAddDevice}><Plus />{t('devices.list.add')}</Button>
          : undefined}
      >
        {mine.map((each) => <DeviceRow key={each.id} device={each} onOpen={actions.onOpenDevice} />)}
        {mine.length === 0 && <p className="px-5 py-3 text-[12px] text-muted">{t('devices.list.empty')}</p>}
        {owner && !on && reach.state !== 'starting' && mine.length === 1 && onlyHost && (
          <p className="px-5 py-3 text-[12px] text-muted">{t('devices.list.emptyOff')}</p>
        )}
        {on && onlyHost && (
          <div className={mine.length > 0 ? 'border-t border-border' : undefined}>
            <div className="px-5 pb-2 pt-4 text-[13px] font-semibold text-foreground">{t('devices.list.add')}</div>
            <AddDevice inline kind={add ?? 'iphone'} host={reach.host} account={account} actions={actions} />
          </div>
        )}
        {signedIn.length > 1 && (
          <div className="flex items-center justify-between gap-3 px-5 py-2.5">
            <span className="text-[11px] text-muted">{t('devices.list.unrecognised')}</span>
            <Button variant="ghost" size="compact" className="text-destructive-ink" onClick={actions.onSignOutOthers}>
              {t('devices.list.signOutOthers')}
            </Button>
          </div>
        )}
      </SettingsPanel>

      {owner && others.length > 0 && (
        <SettingsPanel title={t('devices.list.others')} description={t('devices.list.othersHint')}>
          {others.map((each) => <DeviceRow key={each.id} device={each} onOpen={actions.onOpenDevice} />)}
        </SettingsPanel>
      )}

      {afterDevices}

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
          title={t('devices.confirm.removeTitle', { name: opened.name })}
          body={t('devices.confirm.removeBody')}
          action={t('devices.confirm.removeAction')}
          actions={actions}
        />
      )}
      {opened && confirm === 'health-off' && (
        <Confirm
          title={t('devices.confirm.healthOffTitle')}
          body={t('devices.confirm.healthOffBody')}
          action={t('devices.confirm.healthOffAction')}
          actions={actions}
        />
      )}
      {confirm === 'turn-off' && (
        <Confirm title={t('devices.confirm.turnOffTitle')} body={turnOffBody} action={t('devices.confirm.turnOffAction')} actions={actions} />
      )}
      {confirm === 'remove-others' && (
        <Confirm
          title={t('devices.confirm.removeOthersTitle')}
          body={t(othersSyncHealth ? 'devices.confirm.removeOthersBodyHealth' : 'devices.confirm.removeOthersBody', {
            count: signedIn.length, account,
          })}
          action={t('devices.confirm.removeOthersAction')}
          actions={actions}
        />
      )}
    </SettingsPageShell>
  );
}
