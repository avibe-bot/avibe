// Settings · Devices · 连接方式 (ui/design: settings/devices-connection/*), the page behind the
// Devices page's 「连接方式」 row. Nothing here is needed by an ordinary user: everything defaults to
// automatic, and the Devices page shows only the path in use. Groups split by whose choice it is:
// the path this device uses (the Avibe App only), what this Avibe offers (the Cloudflare tunnel,
// direct, the relay), and the tunnel's own settings, drawn with Settings · Remote access's own
// controls. A change made over a remote connection that could cut it is applied on trial: unless
// the same device comes back and keeps it, the instance puts the old value back by itself. Last,
// for the owner at the computer or in a browser: the domain, and a plain rename.
// A view: props only.
import type { ReactNode } from 'react';
import { ChevronRight, Route, Stethoscope } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';

import type { RemoteAccessSettings } from '@/context/ApiContext';
import type { TranslationKey } from '@/i18n/types';
import { deviceUsingText } from '@/lib/remoteAccessDirect';
import {
  TunnelAutoRecoverySwitch,
  TunnelIpFamilyControl,
  TunnelOutboundSelect,
  TunnelProfileControl,
  TunnelProtocolControl,
} from '../remoteAccess/TunnelControls';
import { SettingsPageShell } from '../settings/SettingsPageShell';
import { SettingsPanel, ToggleSwitch } from '../settings/SettingsPrimitives';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog';
import { Input } from '../ui/input';
import { BackToDevices } from './DevicesPageView';
import { DOMAIN_SUFFIX } from './rules';
import { timeParts } from './format';
import {
  TRIAL_WINDOW_SECONDS,
  type ConnectionChange,
  type DeviceUsing,
  type DirectPreference,
  type DirectState,
  type Rename,
  type TunnelState,
} from './types';

export type ConnectionPageActions = {
  onPreferenceChange?: (mode: DirectPreference['mode']) => void;
  onDirectChange?: (on: boolean) => void;
  onRelayChange?: (on: boolean) => void;
  onSettingChange?: <K extends keyof RemoteAccessSettings>(key: K, value: RemoteAccessSettings[K]) => void;
  onOptimizeRoute?: () => void;
  onOpenDiagnostics?: () => void;
  onKeepTrial?: () => void;
  onPutBack?: () => void;
  onChangeDomain?: () => void;
  onRenameNameChange?: (name: string) => void;
  onRenameContinue?: () => void;
  onRenameConfirm?: () => void;
  onRenameCancel?: () => void;
  /** Back to the Devices page. */
  onBack?: () => void;
};

export type ConnectionPageViewProps = ConnectionPageActions & {
  /** Where the page is open: at the machine itself, a browser elsewhere, or the Avibe App. */
  viewer: 'local' | 'browser' | 'app';
  tunnel: TunnelState;
  direct: DirectState;
  relay: boolean;
  /** The relay's switch, shown only when the container turns it on; otherwise the row shows its
   *  state only. */
  relayEditable?: boolean;
  /** The Avibe App's own path, as it reports it; null before it has. */
  path?: DeviceUsing | null;
  /** The Avibe App's own path choice. */
  preference?: DirectPreference;
  /** A change is on trial: the same device came back and may keep it for `seconds` more. */
  trial?: { change: ConnectionChange; seconds: number };
  /** A trial ended without this device coming back: the old value is in place again. */
  reverted?: ConnectionChange;
  /** This Avibe's domain, for the owner: the last row of the page, and the way to change it. */
  host?: string;
  rename?: Rename;
  /** A change is being applied: the controls wait. */
  busy?: boolean;
};

type BadgeVariant = 'info' | 'success' | 'secondary' | 'warning';

const directStateKeys = {
  ready: 'devices.connection.available',
  off: 'devices.connection.off',
  'awaiting-permission': 'devices.connection.stateAwaitingPermission',
  denied: 'devices.connection.stateDenied',
  'awaiting-certificate': 'devices.connection.stateAwaitingCertificate',
  'not-running': 'devices.connection.stateNotRunning',
} as const satisfies Record<DirectState, TranslationKey>;

const directWhyKeys = {
  'awaiting-permission': 'devices.connection.whyAwaitingPermission',
  denied: 'devices.connection.whyDenied',
  'awaiting-certificate': 'devices.connection.whyAwaitingCertificate',
  'not-running': 'devices.connection.whyNotRunning',
} as const satisfies Partial<Record<DirectState, TranslationKey>>;

const settingKeys = {
  protocol: 'remoteAccess.protocol',
  ipFamily: 'remoteAccess.ipFamily',
  profile: 'remoteAccess.optimizationProfile',
  outbound: 'remoteAccess.outboundInterface',
  autoRecovery: 'remoteAccess.autoRecovery',
  direct: 'remoteAccess.direct.label',
  relay: 'remoteAccess.direct.usingRelay',
} as const satisfies Record<ConnectionChange['setting'], TranslationKey>;

const ipKeys = { auto: 'remoteAccess.ipAuto', '4': 'remoteAccess.ipV4', '6': 'remoteAccess.ipV6' } as const satisfies Record<RemoteAccessSettings['edge_ip_version'], TranslationKey>;
const profileKeys = {
  stable: 'remoteAccess.profileStable',
  balanced: 'remoteAccess.profileBalanced',
  low_latency: 'remoteAccess.profileLowLatency',
} as const satisfies Record<RemoteAccessSettings['optimization_profile'], TranslationKey>;

const revertedKeys = {
  today: 'devices.connection.choiceRevertedToday',
  yesterday: 'devices.connection.choiceRevertedYesterday',
  other: 'devices.connection.choiceRevertedDated',
} as const satisfies Record<NonNullable<ReturnType<typeof timeParts>>['day'], TranslationKey>;

const protocolName = (protocol: 'quic' | 'http2') => (protocol === 'quic' ? 'QUIC' : 'HTTP/2');

/** One side of a change, as the page's own controls name it. */
function changeValue(t: TFunction, change: ConnectionChange, side: 'to' | 'from'): string {
  switch (change.setting) {
    case 'protocol': {
      const value = change[side];
      return value === 'auto' ? t('remoteAccess.protocolAuto') : protocolName(value);
    }
    case 'ipFamily':
      return t(ipKeys[change[side]]);
    case 'profile':
      return t(profileKeys[change[side]]);
    case 'outbound':
      return change[side] || t('remoteAccess.outboundSystem');
    default:
      return t(change[side] ? 'devices.connection.valueOn' : 'devices.connection.valueOff');
  }
}

const changeParams = (t: TFunction, change: ConnectionChange) => ({
  setting: t(settingKeys[change.setting]),
  value: changeValue(t, change, 'to'),
  back: changeValue(t, change, 'from'),
});

function PathRow({ name, detail, state, why, ms, control }: {
  name: string; detail: string; state: { label: string; variant: BadgeVariant }; why?: string; ms?: number | null; control?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className={`flex items-center gap-3 border-b border-border px-5 py-3 last:border-b-0 ${state.variant === 'info' ? 'bg-cyan/[0.06]' : ''}`}>
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-foreground">{name}</div>
        <div className={`mt-0.5 text-[11px] leading-relaxed ${why ? 'text-gold-ink' : 'text-muted'}`}>{why ?? detail}</div>
      </div>
      {typeof ms === 'number' && <span className="shrink-0 text-[11px] tabular-nums text-muted">{t('remoteAccess.direct.legMs', { ms })}</span>}
      <Badge variant={state.variant} className="shrink-0">{state.label}</Badge>
      {control && <div className="shrink-0">{control}</div>}
    </div>
  );
}

function Setting({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-2 border-b border-border px-5 py-3 last:border-b-0 md:flex-row md:items-center md:justify-between md:gap-6">
      <div className="min-w-0">
        <div className="text-[13px] font-medium text-foreground">{title}</div>
        {hint && <div className="mt-0.5 text-[11px] leading-relaxed text-muted">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function NavRow({ icon: Icon, label, onClick }: { icon: typeof Route; label: string; onClick?: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-center gap-3 border-b border-border px-5 py-3 text-left text-[13px] text-foreground transition-colors last:border-b-0 hover:bg-field/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
    >
      <Icon className="size-4 shrink-0 text-muted" />
      <span className="flex-1">{label}</span>
      <ChevronRight className="size-4 text-muted" />
    </button>
  );
}

/** The Avibe App's own choice: automatic, or the Cloudflare tunnel only. */
function PreferenceGroup({ path, preference, onChange }: {
  path?: DeviceUsing | null; preference: DirectPreference; onChange?: (mode: DirectPreference['mode']) => void;
}) {
  const { t, i18n } = useTranslation();
  const options = [
    ['automatic', t('devices.connection.automatic'), t('devices.connection.automaticHint')],
    ['tunnel', t('devices.connection.tunnelOnly'), t('devices.connection.tunnelOnlyHint')],
  ] as const;
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
                <span className="block text-[11px] leading-relaxed text-muted">{text}</span>
              </span>
            </button>
          );
        })}
      </div>
      <p className="border-t border-border px-5 py-2.5 text-[11px] leading-relaxed text-muted">{t('devices.connection.choiceHint')}</p>
      {preference.reverted && (
        <p role="status" className="border-t border-border bg-gold/10 px-5 py-2.5 text-[11px] leading-relaxed text-foreground">
          {(() => {
            const parts = timeParts(preference.reverted.at, i18n.language);
            return parts ? t(revertedKeys[parts.day], { date: parts.date, time: parts.time }) : t('devices.connection.choiceRevertedUndated');
          })()}
        </p>
      )}
    </SettingsPanel>
  );
}

function RenameDialogs({ host, rename, actions }: { host: string; rename: Rename; actions: ConnectionPageActions }) {
  const { t } = useTranslation();
  const close = (open: boolean) => { if (!open) actions.onRenameCancel?.(); };
  if (rename.step === 'form') {
    const taken = rename.nameState === 'taken';
    return (
      <Dialog open onOpenChange={close}>
        <DialogContent className="max-w-[480px]" closeLabel={t('common.close')}>
          <DialogHeader>
            <DialogTitle>{t('devices.rename.title')}</DialogTitle>
            <DialogDescription>{t('devices.rename.current', { host })}</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <div className="flex min-w-0 items-center gap-2">
              <Input
                className="w-48 text-[13px]"
                value={rename.name}
                readOnly={!actions.onRenameNameChange}
                onChange={actions.onRenameNameChange ? (event) => actions.onRenameNameChange?.(event.target.value) : undefined}
                aria-label={t('devices.rename.newName')}
                aria-invalid={taken || undefined}
              />
              <span className="truncate font-mono text-[12px] text-muted">{t('devices.claim.suffix')}</span>
            </div>
            {taken && <span className="text-[11px] text-destructive-ink">{t('devices.claim.taken')}</span>}
          </div>
          <div className="rounded-lg bg-field px-4 py-3">
            <div className="text-[12px] font-semibold text-foreground">{t('devices.rename.after')}</div>
            <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-[12px] leading-relaxed text-muted">
              <li>{t('devices.rename.void', { host, days: rename.voidDays })}</li>
              <li>{t('devices.rename.signedOut')}</li>
              <li>{t('devices.rename.links')}</li>
              <li>{t('devices.rename.homeScreen')}</li>
              {rename.health && <li>{t('devices.rename.health')}</li>}
              <li>{t('devices.rename.direct')}</li>
            </ul>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={actions.onRenameCancel}>{t('common.cancel')}</Button>
            <Button variant="default" disabled={taken || !rename.name} onClick={actions.onRenameContinue}>{t('common.continue')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  if (rename.step === 'confirm') {
    const waiting = rename.countdown > 0;
    return (
      <Dialog open onOpenChange={close}>
        <DialogContent className="max-w-md" closeLabel={t('common.close')}>
          <DialogHeader>
            <DialogTitle>{t('devices.rename.confirmTitle', { host: `${rename.name}${DOMAIN_SUFFIX}` })}</DialogTitle>
            <DialogDescription>{t('devices.rename.confirmBody', { host })}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={actions.onRenameCancel}>{t('common.cancel')}</Button>
            <Button variant="destructive" disabled={waiting} onClick={actions.onRenameConfirm}>
              {waiting ? t('devices.rename.confirmCountdown', { seconds: rename.countdown }) : t('devices.rename.confirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  return null;
}

export function ConnectionPageView({
  viewer, tunnel, direct, relay, relayEditable = false, path, preference, trial, reverted, host, rename, busy, ...actions
}: ConnectionPageViewProps) {
  const { t } = useTranslation();
  const remote = viewer !== 'local';
  const inUse = { label: t('devices.connection.inUse'), variant: 'info' as const };
  const available = { label: t('devices.connection.available'), variant: 'success' as const };
  const directState = direct === 'ready'
    ? available
    : { label: t(directStateKeys[direct]), variant: direct === 'off' ? 'secondary' as const : 'warning' as const };
  const why = direct in directWhyKeys ? t(directWhyKeys[direct as keyof typeof directWhyKeys]) : undefined;
  const relayState = !relay ? { label: t('devices.connection.off'), variant: 'secondary' as const } : directState;
  const using = (kind: DeviceUsing['kind']) => path?.kind === kind;
  const rtt = (kind: DeviceUsing['kind']) => (path && path.kind === kind && 'rttMs' in path ? path.rttMs : null);
  const { settings } = tunnel;
  const set = actions.onSettingChange;

  return (
    <SettingsPageShell
      title={t('devices.connection.title')}
      subtitle={t('devices.connection.subtitle')}
      activeTab="remote-access"
    >
      <BackToDevices onBack={actions.onBack} />
      {rename?.step === 'done' && host && (
        <div role="status" className="rounded-xl border border-mint/40 bg-mint-soft px-5 py-3 text-[12px] leading-relaxed text-foreground">
          {t('devices.connection.renamed', { host, from: rename.from })}
        </div>
      )}
      {reverted && (
        <div role="status" className="rounded-xl border border-gold/40 bg-gold/10 px-5 py-3 text-[12px] leading-relaxed text-foreground">
          {t('devices.connection.reverted', changeParams(t, reverted))}
        </div>
      )}

      {viewer === 'app' && preference && (
        <PreferenceGroup path={path} preference={preference} onChange={actions.onPreferenceChange} />
      )}

      <SettingsPanel
        title={t('devices.connection.offered')}
        description={viewer === 'app' ? undefined : t('devices.connection.offeredBrowser')}
      >
        <PathRow
          name={t('remoteAccess.direct.usingTunnel')}
          detail={t('devices.connection.tunnelDetail')}
          // A browser elsewhere comes in through the tunnel; the computer itself does not.
          state={viewer === 'browser' || using('tunnel') ? inUse : available}
          ms={tunnel.ms}
        />
        <div className="flex items-center justify-between gap-4 border-b border-border px-5 py-3">
          <div className="min-w-0">
            <div className="text-[13px] font-medium text-foreground">{t('remoteAccess.direct.label')}</div>
            <div className="mt-0.5 text-[11px] leading-relaxed text-muted">{t('devices.connection.directDetail')}</div>
          </div>
          <ToggleSwitch
            enabled={direct !== 'off'}
            disabled={busy}
            onClick={() => actions.onDirectChange?.(direct === 'off')}
            label={t('remoteAccess.direct.label')}
          />
        </div>
        <PathRow
          name={t('remoteAccess.direct.usingLan')}
          detail={t('devices.connection.lanDetail')}
          state={using('lan') ? inUse : directState}
          why={why}
          ms={rtt('lan')}
        />
        <PathRow
          name={t('remoteAccess.direct.usingPunched')}
          detail={t('devices.connection.punchedDetail')}
          state={using('punched') ? inUse : directState}
          why={why}
          ms={rtt('punched')}
        />
        <PathRow
          name={t('remoteAccess.direct.usingRelay')}
          detail={t('devices.connection.relayDetail')}
          state={using('relay') ? inUse : relayState}
          why={relay ? why : undefined}
          ms={rtt('relay')}
          control={relayEditable ? (
            <ToggleSwitch
              enabled={relay}
              disabled={busy || direct === 'off'}
              onClick={() => actions.onRelayChange?.(!relay)}
              label={t('remoteAccess.direct.usingRelay')}
            />
          ) : undefined}
        />
      </SettingsPanel>

      <SettingsPanel
        title={t('devices.connection.tunnelSettings')}
        description={remote ? t('devices.connection.trialNote', { seconds: TRIAL_WINDOW_SECONDS }) : t('devices.connection.localNote')}
      >
        <Setting
          title={t('remoteAccess.protocol')}
          hint={settings.transport_protocol === 'auto'
            ? tunnel.active ? t('devices.connection.protocolAutomatic', { protocol: protocolName(tunnel.active) }) : undefined
            : t('devices.connection.protocolPinned')}
        >
          <TunnelProtocolControl value={settings.transport_protocol} onChange={(value) => set?.('transport_protocol', value)} disabled={busy} />
        </Setting>
        <Setting title={t('remoteAccess.ipFamily')}>
          <TunnelIpFamilyControl value={settings.edge_ip_version} onChange={(value) => set?.('edge_ip_version', value)} disabled={busy} />
        </Setting>
        <Setting title={t('remoteAccess.outboundInterface')}>
          <TunnelOutboundSelect
            value={settings.edge_bind_address}
            onChange={(value) => set?.('edge_bind_address', value)}
            disabled={busy}
            interfaces={tunnel.interfaces}
            className="md:w-56"
          />
        </Setting>
        <Setting title={t('remoteAccess.autoRecovery')}>
          <TunnelAutoRecoverySwitch value={settings.auto_recovery} onChange={(value) => set?.('auto_recovery', value)} disabled={busy} />
        </Setting>
        <Setting title={t('remoteAccess.optimizationProfile')}>
          <TunnelProfileControl value={settings.optimization_profile} onChange={(value) => set?.('optimization_profile', value)} disabled={busy} />
        </Setting>
      </SettingsPanel>

      <SettingsPanel>
        <NavRow icon={Route} label={t('remoteAccess.optimizeRoute')} onClick={actions.onOptimizeRoute} />
        <NavRow icon={Stethoscope} label={t('devices.connection.diagnostics')} onClick={actions.onOpenDiagnostics} />
      </SettingsPanel>

      {host && viewer !== 'app' && (
        <SettingsPanel>
          <div className="flex items-center gap-3 px-5 py-3">
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-foreground">{t('devices.connection.domain')}</div>
              <div className="mt-0.5 truncate font-mono text-[11px] text-muted">{host}</div>
            </div>
            <Button variant="ghost" size="compact" className="text-destructive-ink" onClick={actions.onChangeDomain}>
              {t('devices.connection.changeDomain')}
            </Button>
          </div>
        </SettingsPanel>
      )}

      {rename && host && viewer !== 'app' && <RenameDialogs host={host} rename={rename} actions={actions} />}

      {trial && (
        <Dialog open onOpenChange={(open) => { if (!open) actions.onPutBack?.(); }}>
          <DialogContent className="max-w-md" closeLabel={t('common.close')}>
            <DialogHeader>
              <DialogTitle>{t('devices.connection.trialTitle')}</DialogTitle>
              <DialogDescription>
                {t('devices.connection.trialBody', { ...changeParams(t, trial.change), seconds: trial.seconds })}
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="ghost" onClick={actions.onPutBack}>{t('devices.connection.putBack')}</Button>
              <Button variant="default" onClick={actions.onKeepTrial}>{t('devices.connection.keep', { seconds: trial.seconds })}</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </SettingsPageShell>
  );
}
