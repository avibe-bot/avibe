// What the Devices views decide from their data, kept apart from the markup so the containers can
// ask the same questions (does this need a confirmation?) before they open one.
import type { TFunction } from 'i18next';

import type { TranslationKey } from '@/i18n/types';
import { dayText, healthText } from './format';
import { instanceUrl } from './paths';
import type { Device, DeviceActivity, DeviceFunction, RenameDevice, TunnelSetting } from './types';

/** The Avibe App's TestFlight invitation; TestFlight lists the App as 「Avibe OS」. */
export const TESTFLIGHT_INVITE_URL = 'https://testflight.apple.com/join/HXqwMqeX';
import { DOMAIN_SUFFIX } from '../DomainField';

/** Every domain this Avibe can claim ends so (one source: DomainField). */
export { DOMAIN_SUFFIX };
/** The name part of a domain this Avibe can claim, or the host as it is. */
export const prefixOf = (host: string) => (host.endsWith(DOMAIN_SUFFIX) ? host.slice(0, -DOMAIN_SUFFIX.length) : host);

/** Signing a device out loses something only when it synced health data (whether or not Health
 *  is still on): then it asks first. */
export const signOutNeedsConfirm = (device: Device) =>
  device.functions.some((each) => each.kind === 'health' && (each.on || Boolean(each.syncedAt)));

/** Turning a device's Health off deletes what it synced (`mobile_store.revoke` purges it), and only
 *  the iPhone can connect it again: it always asks first. */
export const functionOffNeedsConfirm = (fn: DeviceFunction) => fn.kind === 'health';

/** Signing out every other device always asks first. */
export const signOutOthersNeedsConfirm = () => true;

/** Turning remote access off from anywhere but the computer that runs Avibe locks this device out:
 *  it asks first unless this device is known to be that computer. */
export const turnOffNeedsConfirm = (devices: Device[]) => devices.find((each) => each.self)?.kind !== 'host';

/**
 * How a function shows in a device's detail: a switch, or its state only. The instance says whether
 * this viewer may change it; Computer Use is never changed here; Health, once off, connects again
 * only on the iPhone. A switch that would be refused when pressed is never drawn.
 */
export const functionControl = (fn: DeviceFunction): 'switch' | 'state' => {
  if (fn.kind === 'computerUse' || !fn.canChange) return 'state';
  if (fn.kind === 'health' && !fn.on) return 'state';
  return 'switch';
};

/** My own signed-in devices other than this one and the computer that runs Avibe. */
export const othersSignedIn = (devices: Device[]) =>
  devices.filter((each) => each.group === 'mine' && each.kind !== 'host' && !each.self);

/** Every remote device, mine or not (what the computer itself can sign out at once). */
export const remoteDevices = (devices: Device[]) => devices.filter((each) => each.kind !== 'host');

/** How many of everyone's devices besides this one turning remote access off locks out: all but
 *  the host. While this device is not known it is one of them, so it is not counted twice. */
export const lockedOutCount = (devices: Device[]) => {
  const remote = devices.filter((each) => each.group !== 'unattributed' && each.kind !== 'host' && !each.self && !each.pushOnly);
  return devices.some((each) => each.self) ? remote.length : Math.max(0, remote.length - 1);
};

/** The latest entries first, whatever order they came in. */
export const newestFirst = (items: DeviceActivity[]) =>
  [...items].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));

/** How many entries the Devices page itself shows; the rest are in the activity list. */
export const RECENT_ACTIVITY_LIMIT = 5;

/** One activity entry, in the page's own words. A device no longer listed is said as such. */
export function activityText(t: TFunction, language: string, item: DeviceActivity): string {
  const { event } = item;
  // A device no longer listed has its own sentences: in Chinese a name is set off by a space, a phrase is not.
  const device = item.device;
  switch (event.kind) {
    case 'signedIn':
      return device ? t('devices.activity.signedIn', { device }) : t('devices.activity.formerSignedIn');
    case 'signedOut':
      if (event.actor) {
        return device
          ? t('devices.activity.signedOutBy', { device, actor: event.actor })
          : t('devices.activity.formerSignedOutBy', { actor: event.actor });
      }
      return device ? t('devices.activity.signedOut', { device }) : t('devices.activity.formerSignedOut');
    case 'healthRead': {
      const data = healthText(t, language, event.data);
      const days = event.fromDay === event.toDay
        ? t('devices.activity.day', { day: dayText(event.fromDay, language) })
        : t('devices.activity.days', { from: dayText(event.fromDay, language), to: dayText(event.toDay, language) });
      return event.task
        ? t('devices.activity.healthReadFor', { days, data, task: event.task })
        : t('devices.activity.healthRead', { days, data });
    }
    case 'alarmCreated':
    case 'reminderCreated': {
      const alarm = event.kind === 'alarmCreated';
      const what = event.label
        ? t(alarm ? 'devices.activity.alarmLabelled' : 'devices.activity.reminderLabelled', { label: event.label })
        : t(alarm ? 'devices.activity.alarm' : 'devices.activity.reminder');
      if (!event.confirmed) return what;
      return device ? t('devices.activity.confirmedOn', { what, device }) : t('devices.activity.formerConfirmedOn', { what });
    }
    case 'remoteAccessOn':
      return t('devices.activity.remoteAccessOn');
    case 'remoteAccessOff':
      return t('devices.activity.remoteAccessOff');
  }
}

/** A tunnel setting's stored value, as its own control names it. */
export function settingValueText(t: TFunction, setting: TunnelSetting, raw: string): string {
  switch (setting) {
    case 'protocol':
      return raw === 'auto' ? t('remoteAccess.protocolAuto') : raw === 'quic' ? 'QUIC' : raw === 'http2' ? 'HTTP/2' : raw;
    case 'ipFamily':
      return raw === 'auto' ? t('remoteAccess.ipAuto') : raw === '4' ? t('remoteAccess.ipV4') : raw === '6' ? t('remoteAccess.ipV6') : raw;
    case 'outbound':
      return raw || t('remoteAccess.outboundSystem');
  }
}

const settingNameKeys = {
  protocol: 'remoteAccess.protocol',
  ipFamily: 'remoteAccess.ipFamily',
  outbound: 'remoteAccess.outboundInterface',
} as const satisfies Record<TunnelSetting, TranslationKey>;
export const settingName = (t: TFunction, setting: TunnelSetting) => t(settingNameKeys[setting]);

/** The devices a rename lists before it happens: the viewer's own, each with how to sign out there. */
export const renameDevices = (devices: Device[]): RenameDevice[] => devices
  .filter((each) => each.group === 'mine')
  .map((each) => ({
    id: each.id,
    name: each.name,
    kind: each.client.kind === 'homeScreen' ? 'homeScreen' : each.kind,
    self: each.self,
    pushOnly: each.pushOnly,
  }));

export { instanceUrl };
