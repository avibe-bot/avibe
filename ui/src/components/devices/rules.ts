// What the Devices views decide from their data, kept apart from the markup so the containers can
// ask the same questions (does this need a confirmation?) before they open one.
import type { TFunction } from 'i18next';

import { healthText } from './format';
import type { Device, DeviceActivity } from './types';

/** The Avibe App's TestFlight invitation; TestFlight lists the App as 「Avibe OS」. */
export const TESTFLIGHT_INVITE_URL = 'https://testflight.apple.com/join/HXqwMqeX';
/** Every domain this Avibe can claim ends so. */
export const DOMAIN_SUFFIX = '-app.avibe.bot';

/** Signing a device out loses something only when it synced health data: then it asks first. */
export const signOutNeedsConfirm = (device: Device) =>
  device.capabilities.some((each) => each.kind === 'health' && each.on);

/** Turning remote access off from another device locks that device out: then it asks first. */
export const turnOffNeedsConfirm = (devices: Device[]) => {
  const self = devices.find((each) => each.self);
  return Boolean(self && self.kind !== 'host');
};

/** My own signed-in devices other than this one and the computer that runs Avibe. */
export const othersSignedIn = (devices: Device[]) => devices.filter((each) => !each.person && each.kind !== 'host' && !each.self);

/** One activity entry, in the page's own words. */
export function activityText(t: TFunction, item: DeviceActivity): string {
  const { event } = item;
  switch (event.kind) {
    case 'signedIn':
      return t('devices.activity.signedIn', { device: event.device });
    case 'signedOut':
      return t('devices.activity.signedOut', { device: event.device });
    case 'healthRead':
      return event.task
        ? t('devices.activity.healthReadFor', { days: event.days, data: healthText(t, event.data), task: event.task })
        : t('devices.activity.healthRead', { days: event.days, data: healthText(t, event.data) });
    case 'healthConnected':
      return t('devices.activity.healthConnected', { device: event.device, data: healthText(t, event.data) });
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
