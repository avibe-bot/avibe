// Changing this Avibe's domain (ui/design: settings/devices-rename/*), opened from 「连接方式」.
// The old name stops working at once and is kept for this account until the day avibe.bot gives;
// afterwards anyone may claim it. Renaming clears what it can by itself and says plainly what it
// cannot reach: the list never claims that everything is clean. A view: props only.
import { Check, Globe, Laptop, LoaderCircle, Monitor, MonitorSmartphone, RotateCcw, Smartphone, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { TranslationKey } from '@/i18n/types';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../ui/dialog';
import { DomainField } from '../DomainField';
import { validDomainName } from '../domainName';
import { KeptTail } from '../ui/kept-tail';
import { deviceTime, joinParts, longDay } from './format';
import { prefixOf } from './rules';
import { renameRefusalText } from './texts';
import type { AddressState, RenameDevice, RenameDeviceKind, RenameStep, RetiredName } from './types';

export type RenameActions = {
  onRenameNameChange?: (name: string) => void;
  onRenameContinue?: () => void;
  onRenameConfirm?: () => void;
  /** Close without changing anything (or leave the list afterwards). */
  onRenameClose?: () => void;
};

const RETRYABLE = new Set(['unavailable', 'confirmation_expired', 'operation_in_progress', 'address_changed', 'unknown']);
const deviceIcon = { host: Laptop, iphone: Smartphone, desktop: Monitor, browser: Globe, homeScreen: Globe, other: MonitorSmartphone } as const;
const kindNameKeys = {
  host: 'devices.kindName.runtime',
  iphone: 'devices.kindName.app',
  desktop: 'devices.kindName.desktop',
  browser: 'devices.kindName.browser',
  homeScreen: 'devices.kindName.homeScreen',
  other: 'devices.kindName.other',
} as const satisfies Record<RenameDeviceKind, TranslationKey>;
const signOutKeys = {
  host: 'devices.rename.howHost',
  iphone: 'devices.rename.howIphone',
  desktop: 'devices.rename.howDesktop',
  homeScreen: 'devices.rename.howHomeScreen',
  browser: 'devices.rename.howBrowser',
  other: 'devices.rename.howOther',
} as const satisfies Record<RenameDeviceKind, TranslationKey>;

/** The one thing renaming cannot reach. Never small print. */
function Residue({ host, releaseAt, after }: { host: string; releaseAt?: string; after: boolean }) {
  const { t, i18n } = useTranslation();
  const date = releaseAt ? longDay(releaseAt, i18n.language) : null;
  const text = date
    ? t(after ? 'devices.rename.residueAfter' : 'devices.rename.residueBefore', { host, date })
    : t(after ? 'devices.rename.residueAfterUndated' : 'devices.rename.residueBeforeUndated', { host });
  return (
    <div role="note" className="flex gap-3 rounded-lg border border-gold/40 bg-gold/10 px-4 py-3">
      <TriangleAlert className="mt-0.5 size-4 shrink-0 text-gold-ink" />
      <p className="text-[13px] leading-relaxed text-foreground"><KeptTail text={text} /></p>
    </div>
  );
}

/** The devices, one row each: before the rename how to sign out there, after it whether it was cleared. */
function DeviceChecklist({ devices, host, after }: { devices: RenameDevice[]; host: string; after: boolean }) {
  const { t, i18n } = useTranslation();
  const others = devices.some((each) => each.person);
  return (
    <div className="overflow-hidden rounded-lg border border-edge">
      <div className="border-b border-border bg-field/60 px-4 py-2 text-[12px] font-semibold text-foreground">
        {after ? t(others ? 'devices.rename.devicesAll' : 'devices.rename.devicesAfter') : t('devices.rename.devicesBefore')}
      </div>
      {devices.length === 0 && <p className="px-4 py-2.5 text-[12px] text-muted">{t('devices.rename.devicesNone')}</p>}
      {devices.map((device) => {
        const Icon = deviceIcon[device.kind];
        const how = device.self && device.kind !== 'host'
          ? t('devices.rename.howSelf')
          : device.pushOnly ? t('devices.rename.howPushOnly') : t(signOutKeys[device.kind], { host });
        return (
          <div key={device.id} className="flex items-start gap-3 border-b border-border px-4 py-2.5 last:border-b-0">
            <Icon className="mt-0.5 size-4 shrink-0 text-muted" />
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-foreground">{device.name ?? t(kindNameKeys[device.kind])}</div>
              {device.person && <div className="truncate text-[11px] text-muted">{device.person}</div>}
              {!after && <div className="text-[11px] leading-relaxed text-muted"><KeptTail text={how} /></div>}
            </div>
            {after && (device.cleanup === 'cleared' ? (
              <Badge variant="success" className="shrink-0"><Check className="size-3" />{t('devices.rename.cleared')}</Badge>
            ) : (
              <span className="shrink-0 pt-0.5 text-right text-[11px] text-muted">
                {joinParts(t, [
                  t('devices.rename.unconfirmed'),
                  device.lastActiveAt ? t('devices.rename.lastActive', { when: deviceTime(device.lastActiveAt, t, i18n.language, 'date') }) : null,
                ])}
              </span>
            ))}
          </div>
        );
      })}
      <p className="border-t border-border px-4 py-2 text-[11px] leading-relaxed text-muted"><KeptTail text={t('devices.rename.listNote')} /></p>
    </div>
  );
}

function Consequences({ host, address }: { host: string; address: AddressState }) {
  const { t, i18n } = useTranslation();
  const date = address.releaseAtIfChangedNow ? longDay(address.releaseAtIfChangedNow, i18n.language) : null;
  const kept = date
    ? t('devices.rename.keptUntil', { host, date })
    : typeof address.holdDays === 'number'
      ? t('devices.rename.keptDays', { count: address.holdDays })
      : t('devices.rename.keptPlain');
  const items = [
    t('devices.rename.signedOut'),
    t('devices.rename.links'),
    t('devices.rename.notifications'),
    address.healthConnected ? t('devices.rename.health') : null,
    kept,
  ].filter((each): each is string => each !== null);
  return (
    <div className="rounded-lg bg-field px-4 py-3">
      <div className="text-[12px] font-semibold text-foreground">{t('devices.rename.after')}</div>
      <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-[12px] leading-relaxed text-muted">
        {items.map((item) => <li key={item}><KeptTail text={item} /></li>)}
      </ul>
    </div>
  );
}

/** The list for a name that was given up: just now (`kept`, said first), or looked at again later. */
function CleanupDialog({ title, oldHost, retired, kept, onClose }: {
  title: string; oldHost: string; retired: RetiredName | null; kept?: boolean; onClose?: () => void;
}) {
  const { t, i18n } = useTranslation();
  // Just renamed, the dialog says first that the old name is kept for this account, until the day
  // the instance reports (or without a day where it reports none); the clean-up comes after.
  const keptText = retired?.releaseAt
    ? t('devices.rename.keptUntil', { host: oldHost, date: longDay(retired.releaseAt, i18n.language) })
    : t('devices.rename.keptPlain');
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose?.(); }}>
      <DialogContent className="max-w-lg content-start" mobileSheetHeight="tall" closeLabel={t('common.close')}>
        <DialogHeader>
          <DialogTitle className="text-balance">{title}</DialogTitle>
          {kept && <p className="text-[13px] leading-relaxed text-foreground"><KeptTail text={keptText} /></p>}
          <DialogDescription><KeptTail text={t('devices.rename.cleaning')} /></DialogDescription>
        </DialogHeader>
        {retired
          ? <DeviceChecklist devices={retired.devices} host={oldHost} after />
          : <p className="text-[12px] leading-relaxed text-muted"><KeptTail text={t('devices.rename.listUnread')} /></p>}
        <Residue host={oldHost} releaseAt={retired?.releaseAt} after />
        <DialogFooter>
          <Button variant="default" onClick={onClose}>{t('common.done')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function RenameDialogs({ host, rename, address, devices, rebindOffered = true, actions }: {
  /** The domain now. */
  host: string;
  rename: RenameStep;
  address: AddressState;
  /** The viewer's devices, listed before the rename. */
  devices: RenameDevice[];
  /** This instance has binding again: a refusal may point there. */
  rebindOffered?: boolean;
  actions: RenameActions;
}) {
  const { t, i18n } = useTranslation();
  const close = (open: boolean) => { if (!open) actions.onRenameClose?.(); };
  if (rename.step === 'cleanup') {
    return (
      <CleanupDialog
        title={t('devices.rename.cleanupTitle', { host: rename.retired.hostname })}
        oldHost={rename.retired.hostname}
        retired={rename.retired}
        onClose={actions.onRenameClose}
      />
    );
  }
  if (rename.step === 'done') {
    return (
      <CleanupDialog
        title={t('devices.rename.doneTitle', { host: rename.hostname })}
        oldHost={rename.previous}
        retired={rename.retired}
        kept
        onClose={actions.onRenameClose}
      />
    );
  }
  if (rename.step === 'pending') {
    return (
      <Dialog open onOpenChange={close}>
        <DialogContent className="max-w-md" closeLabel={t('common.close')}>
          <DialogHeader>
            <DialogTitle className="text-balance">{t('devices.rename.pendingTitle', { host: rename.hostname })}</DialogTitle>
            <DialogDescription><KeptTail text={t('devices.rename.pendingBody')} /></DialogDescription>
          </DialogHeader>
          <div role="status" className="flex items-center gap-2 text-[12px] text-muted">
            <LoaderCircle className="size-4 motion-safe:animate-spin" />
            {t('devices.rename.pending')}
          </div>
        </DialogContent>
      </Dialog>
    );
  }
  if (rename.step === 'confirm') {
    const waiting = rename.countdown > 0;
    const date = rename.releaseAt ? longDay(rename.releaseAt, i18n.language) : null;
    const body = date
      ? t('devices.rename.confirmBody', { host, date })
      : typeof rename.holdDays === 'number'
        ? t('devices.rename.confirmBodyDays', { host, count: rename.holdDays })
        : t('devices.rename.confirmBodyPlain', { host });
    return (
      <Dialog open onOpenChange={close}>
        <DialogContent className="max-w-md" closeLabel={t('common.close')}>
          <DialogHeader>
            <DialogTitle className="text-balance">{t('devices.rename.confirmTitle', { host: rename.hostname })}</DialogTitle>
            <DialogDescription><KeptTail text={body} /></DialogDescription>
          </DialogHeader>
          {rename.refusal && (
            <p role="alert" className="text-[12px] leading-relaxed text-destructive-ink">
              <KeptTail text={renameRefusalText(t, i18n.language, rename.refusal, rebindOffered)} />
            </p>
          )}
          <DialogFooter>
            <Button variant="ghost" onClick={actions.onRenameClose}>{t('common.cancel')}</Button>
            <Button variant="destructive" disabled={waiting || rename.busy} onClick={actions.onRenameConfirm}>
              {waiting ? t('devices.rename.confirmCountdown', { seconds: rename.countdown }) : t('devices.rename.confirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  const { refusal } = rename;
  // A refusal about the name or the Avibe itself stops here until the name changes; one about the
  // moment (avibe.bot out of reach, an expired confirmation) can simply be tried again.
  const blocking = Boolean(refusal) && !RETRYABLE.has(refusal!.code);
  const reclaiming = address.retired.some((each) => prefixOf(each.hostname) === rename.name);
  const reason = refusal ? renameRefusalText(t, i18n.language, refusal, rebindOffered) : null;
  return (
    <Dialog open onOpenChange={close}>
      <DialogContent className="max-w-xl content-start" mobileSheetHeight="tall" closeLabel={t('common.close')}>
        <DialogHeader>
          <DialogTitle>{t('devices.rename.title')}</DialogTitle>
          <DialogDescription>{t('devices.rename.current', { host })}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-2">
          {/* The same field, and the same clean-up, as claiming this computer's domain. */}
          <DomainField
            name={rename.name}
            // A refusal (「已被占用」 among them) is said once, in its sentence below.
            check={rename.name && !validDomainName(rename.name) ? 'invalid' : null}
            readOnly={!actions.onRenameNameChange}
            onChange={actions.onRenameNameChange}
            label={t('devices.rename.newName')}
          />
          {reason && (
            <span role="alert" className="text-[11px] leading-relaxed text-destructive-ink">
              <KeptTail text={rename.earlier ? t('devices.rename.earlierFailed', { reason }) : reason} />
            </span>
          )}
          {reclaiming && !refusal && <span className="text-[11px] leading-relaxed text-mint-ink">{t('devices.rename.reclaiming')}</span>}
          {address.retired.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <span className="text-[11px] text-muted">{t('devices.rename.takeBack')}</span>
              <div className="flex flex-wrap gap-2">
                {address.retired.map((each) => {
                  const name = prefixOf(each.hostname);
                  const picked = name === rename.name;
                  return (
                    <button
                      key={each.hostname}
                      type="button"
                      aria-pressed={picked}
                      onClick={() => actions.onRenameNameChange?.(name)}
                      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-[12px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${picked ? 'border-mint bg-mint-soft text-mint-ink' : 'border-edge text-foreground'}`}
                    >
                      <RotateCcw className="size-3" />
                      <span className="font-mono">{name}</span>
                      {each.releaseAt && <span className="text-muted">{t('devices.rename.until', { date: longDay(each.releaseAt, i18n.language) })}</span>}
                    </button>
                  );
                })}
              </div>
            </div>
          )}
        </div>
        {/* While refused, what renaming would do is folded away: nothing below can be acted on. */}
        {!blocking && (
          <>
            <Consequences host={host} address={address} />
            <Residue host={host} releaseAt={address.releaseAtIfChangedNow} after={false} />
            <DeviceChecklist devices={devices} host={host} after={false} />
          </>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={actions.onRenameClose}>{t('common.cancel')}</Button>
          <Button variant="default" disabled={blocking || !validDomainName(rename.name) || rename.busy} onClick={actions.onRenameContinue}>
            {reclaiming ? t('devices.rename.takeBackAction') : t('common.continue')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
