// What the Devices views decide from their data, kept apart from the markup so the containers can
// ask the same questions (does this need a confirmation?) before they open one.
import type { TFunction } from 'i18next';

import { healthText } from './format';
import type { Device, DeviceActivity, DeviceCapability } from './types';

/** The Avibe App's TestFlight invitation; TestFlight lists the App as 「Avibe OS」. */
export const TESTFLIGHT_INVITE_URL = 'https://testflight.apple.com/join/HXqwMqeX';
/** The address a phone opens this Avibe at. */
export const instanceUrl = (host: string) => `https://${host}`;
/** Every domain this Avibe can claim ends so. */
export const DOMAIN_SUFFIX = '-app.avibe.bot';

/** Signing a device out loses something only when it synced health data (whether or not Health
 *  is still on): then it asks first. */
export const signOutNeedsConfirm = (device: Device) =>
  device.capabilities.some((each) => each.kind === 'health' && (each.on || Boolean(each.syncedAt)));

/** Turning a device's Health off deletes what it synced (`mobile_store.revoke` purges it), and only
 *  the iPhone can connect it again: it always asks first. */
export const capabilityOffNeedsConfirm = (capability: DeviceCapability) => capability.kind === 'health';

/** Signing out every other device always asks first. */
export const signOutOthersNeedsConfirm = () => true;

/** Turning remote access off from anywhere but the computer that runs Avibe locks this device out:
 *  it asks first unless this device is known to be that computer. */
export const turnOffNeedsConfirm = (devices: Device[]) => devices.find((each) => each.self)?.kind !== 'host';

/**
 * How a function shows in a device's detail: a switch, or its state only. Someone else's device is
 * theirs to change; Computer Use seen from another device changes only on that computer; Health,
 * once off, connects again only on the iPhone.
 */
export const capabilityControl = (device: Device, capability: DeviceCapability): 'switch' | 'state' => {
  if (device.group !== 'mine') return 'state';
  if (capability.kind === 'computerUse' && capability.fixed) return 'state';
  if (capability.kind === 'health' && !capability.on) return 'state';
  return 'switch';
};

/** My own signed-in devices other than this one and the computer that runs Avibe. */
export const othersSignedIn = (devices: Device[]) =>
  devices.filter((each) => each.group === 'mine' && each.kind !== 'host' && !each.self);

/** Everyone's devices that turning remote access off locks out: all but this one and the host. */
export const lockedOut = (devices: Device[]) =>
  devices.filter((each) => each.group !== 'unattributed' && each.kind !== 'host' && !each.self);

/** The latest entries first, whatever order they came in. */
export const newestFirst = (items: DeviceActivity[]) =>
  [...items].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));

/** How many entries the Devices page itself shows; the rest are in the activity list. */
export const RECENT_ACTIVITY_LIMIT = 5;

/** One activity entry, in the page's own words. */
export function activityText(t: TFunction, language: string, item: DeviceActivity): string {
  const { event } = item;
  switch (event.kind) {
    case 'signedIn':
      return t('devices.activity.signedIn', { device: event.device });
    case 'signedOut':
      return t('devices.activity.signedOut', { device: event.device });
    case 'healthRead':
      return event.task
        ? t('devices.activity.healthReadFor', { count: event.days, data: healthText(t, language, event.data), task: event.task })
        : t('devices.activity.healthRead', { count: event.days, data: healthText(t, language, event.data) });
    case 'healthConnected':
      return t('devices.activity.healthConnected', { device: event.device, data: healthText(t, language, event.data) });
    case 'alarmCreated':
      return event.confirmedOn
        ? t('devices.activity.alarmConfirmed', { label: event.label, device: event.confirmedOn })
        : t('devices.activity.alarmCreated', { label: event.label });
    case 'directOn':
      return t('devices.activity.directOn');
    case 'remoteAccessOn':
      return t('devices.activity.remoteAccessOn', { host: event.host });
  }
}
