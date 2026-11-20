// How the Devices views say a time, a client, a path, and a list, in the page's own locale.
import type { TFunction } from 'i18next';

import type { TranslationKey } from '@/i18n/types';
import { formatRelativeTime } from '@/lib/relativeTime';
import type { ClientKind, DeviceClient, DeviceConnection, HealthData, PathKind } from './types';

const DAY = 86_400_000;

const startOfDay = (at: number) => {
  const day = new Date(at);
  day.setHours(0, 0, 0, 0);
  return day.getTime();
};

/** Which day an instant falls on, seen from `now`, with its time and date in the locale. */
export function timeParts(iso: string, language: string, now: number = Date.now()) {
  const at = Date.parse(iso);
  // An instant that does not parse has no time to say: callers leave the time out.
  if (!Number.isFinite(at)) return null;
  const time = new Intl.DateTimeFormat(language, { hour: '2-digit', minute: '2-digit', hour12: false }).format(at);
  const date = new Intl.DateTimeFormat(language, { month: 'short', day: 'numeric' }).format(at);
  const today = startOfDay(now);
  const day = at >= today ? 'today' as const : at >= today - DAY ? 'yesterday' as const : 'other' as const;
  return { at, time, date, day };
}

/**
 * `stamp`: 「今天 09:41」, 「昨天 22:40」, then the date with its time. `date`: the same, without a
 * time once it is older than yesterday. `ago`: 「12 分钟前」 within the day, then as `stamp` for
 * yesterday, then 「3 天前」 for a week, then the date. For a time inside a sentence, use the
 * sentence's own `Today` / `Yesterday` / `Dated` templates with `timeParts` instead.
 */
export function deviceTime(
  iso: string,
  t: TFunction,
  language: string,
  style: 'stamp' | 'date' | 'ago',
  now: number = Date.now(),
): string {
  const parts = timeParts(iso, language, now);
  if (!parts) return iso;
  const { at, time, date, day } = parts;
  if (style === 'ago' && day === 'today') return formatRelativeTime(iso, t, now);
  if (day === 'today') return t('devices.time.today', { time });
  if (day === 'yesterday') return t('devices.time.yesterday', { time });
  if (style === 'ago' && at >= startOfDay(now) - 6 * DAY) return formatRelativeTime(iso, t, now, { alwaysRelative: true });
  return style === 'stamp' ? t('devices.time.dated', { date, time }) : date;
}

const clientKeys = {
  runtime: 'devices.client.runtime',
  app: 'devices.client.app',
  desktop: 'devices.client.desktop',
  browser: 'devices.client.browser',
  homeScreen: 'devices.client.homeScreen',
} as const satisfies Record<ClientKind, TranslationKey>;

/** 「Avibe App 3.2.8 · iOS 26.1」, 「浏览器」. */
export function clientText(t: TFunction, client: DeviceClient): string {
  const name = client.version ? t('devices.client.versioned', { name: t(clientKeys[client.kind]), version: client.version }) : t(clientKeys[client.kind]);
  return joinParts(t, [name, client.platform]);
}

const pathKeys = {
  tunnel: 'remoteAccess.direct.usingTunnel',
  relay: 'remoteAccess.direct.usingRelay',
  lan: 'remoteAccess.direct.usingLan',
  punched: 'remoteAccess.direct.usingPunched',
} as const satisfies Record<PathKind, TranslationKey>;

export const pathName = (t: TFunction, path: PathKind) => t(pathKeys[path]);

/** The path a device is on, with its address family and round trip when known. */
export function connectionText(t: TFunction, connection: DeviceConnection, withDetail: boolean): string {
  let text = connection.path ? pathName(t, connection.path) : t('devices.list.online');
  if (!withDetail) return text;
  if (connection.family && (connection.path === 'lan' || connection.path === 'punched')) {
    const family = t(connection.family === 'ipv4' ? 'remoteAccess.direct.familyIpv4' : 'remoteAccess.direct.familyIpv6');
    text = t('remoteAccess.direct.withDetail', { path: text, detail: family });
  }
  if (connection.ms !== undefined) {
    text = t('remoteAccess.direct.withDetail', { path: text, detail: t('remoteAccess.direct.legMs', { ms: connection.ms }) });
  }
  return text;
}

const healthKeys = {
  steps: 'devices.health.steps',
  activity: 'devices.health.activity',
  workouts: 'devices.health.workouts',
  sleep: 'devices.health.sleep',
} as const satisfies Record<HealthData, TranslationKey>;

/** Parts of one line, 「Avibe App 3.2.8 · iOS 26.1」; empty parts are left out. */
export const joinParts = (t: TFunction, parts: Array<string | null | undefined | false>) =>
  parts.filter(Boolean).join(t('devices.partSeparator'));

/** 「步数、活动和睡眠」, "steps, activity, and sleep": the locale's own conjunction. */
export const healthText = (t: TFunction, language: string, data: HealthData[]) =>
  new Intl.ListFormat(language, { type: 'conjunction' }).format(data.map((each) => t(healthKeys[each])));

/** Capitalise a list that opens a line (English names are lower-case inside a sentence). */
export const upperFirst = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
