import type { TFunction } from 'i18next';

import type { RemoteAccessStatus } from '../context/ApiContext';
import type { TranslationKey } from '../i18n/types';
import type { LiveStreamVia } from './liveStreamVia';
import type { NativeDirectConnection, NativeDirectPathKind, NativeDirectPathSnapshot } from './native';

// What Settings · Remote access says about direct, from current facts only: the instance's own
// state and certificate, and how this page's live stream came in (ui/design:
// settings/remote-access/direct-*). No drop count and no fallback reason: neither the instance nor
// the page can know them truthfully today.
export type DirectLines = {
  stateKey: TranslationKey;
  variant: 'success' | 'warning' | 'destructive' | 'secondary';
  certificateDate: Date | null;
  pageKey: TranslationKey | null;
};

/**
 * Null while direct is turned off. `inApp`: in the App (any `direct` version)
 * the page line is not shown. Only the App can tell the relay from a direct
 * address, so 「本页当前连接：直连」 there could name a relayed path; where
 * the App reports its path, the device line says it instead, and the two
 * can never both be shown.
 */
export function remoteAccessDirectLines(
  direct: RemoteAccessStatus['direct'],
  pageVia: LiveStreamVia,
  now: number,
  inApp = false,
): DirectLines | null {
  if (!direct?.enabled) return null;
  const stateKey: TranslationKey = direct.state === 'running'
    ? 'remoteAccess.direct.stateReady'
    : direct.state === 'waiting-permission'
      ? direct.reason === 'permission_denied'
        ? 'remoteAccess.direct.stateDenied'
        : 'remoteAccess.direct.stateAwaitingPermission'
      : direct.state === 'waiting-certificate'
        ? 'remoteAccess.direct.stateAwaitingCertificate'
        : 'remoteAccess.direct.stateNotRunning';
  // Only a certificate that is still valid has a date worth showing.
  const notAfter = direct.certificate_not_after ? new Date(direct.certificate_not_after) : null;
  const certificateDate = notAfter && notAfter.getTime() > now ? notAfter : null;
  const pageKey: TranslationKey | null = inApp
    ? null
    : pageVia === 'direct'
      ? 'remoteAccess.direct.pageDirect'
      : pageVia === 'other'
        ? 'remoteAccess.direct.pageOther'
        : null;
  const variant: DirectLines['variant'] = direct.state === 'running'
    ? 'success'
    : direct.state === 'waiting-permission' && direct.reason === 'permission_denied'
      ? 'destructive'
      : direct.state === 'waiting-permission' || direct.state === 'waiting-certificate'
        ? 'warning'
        : 'secondary';
  return { stateKey, variant, certificateDate, pageKey };
}

/** What this device's page is using, in the four agreed names. */
export type DeviceUsing =
  | { kind: 'tunnel'; relayUp: boolean }
  | { kind: 'relay'; rttMs: number | null }
  | { kind: 'lan' | 'punched' | 'direct'; family: 'ipv4' | 'ipv6'; rttMs: number | null };

/**
 * In the App: what this page's live stream is using. Whether it is on the
 * direct ingress comes from the stream itself (`pageVia`); which kind of
 * direct path from the App (`direct.status()`): the relay, or an IPv4 or IPv6
 * address, on a local network (`lan`) or not (`punched`), or `direct` when
 * the App does not say. A relayed path is never called direct. Null in a
 * browser, before the stream has said, when the stream is direct but the App
 * names no path for it, and unless the App keeps its path current
 * (`appReportsPath`, `direct` version 3; an older App's path can be stale).
 */
export function deviceUsing(
  snapshot: NativeDirectPathSnapshot | null,
  pageVia: LiveStreamVia,
  appReportsPath: boolean,
): DeviceUsing | null {
  if (!appReportsPath || !snapshot || pageVia === null) return null;
  if (pageVia === 'other') {
    return { kind: 'tunnel', relayUp: snapshot.preferred === 'direct' && snapshot.path === 'relay' };
  }
  if (snapshot.path === 'relay') return { kind: 'relay', rttMs: snapshot.rttMs };
  if (snapshot.path === 'ipv4' || snapshot.path === 'ipv6') {
    const kind = snapshot.scope === 'local' ? 'lan' : snapshot.scope === 'global' ? 'punched' : 'direct';
    return { kind, family: snapshot.path, rttMs: snapshot.rttMs };
  }
  return null;
}

const usingNameKeys = {
  relay: 'remoteAccess.direct.usingRelay',
  lan: 'remoteAccess.direct.usingLan',
  punched: 'remoteAccess.direct.usingPunched',
  direct: 'remoteAccess.direct.usingDirect',
} as const satisfies Record<string, TranslationKey>;

/**
 * The device line: 「本设备正在使用：」 and the path in use by name, then its
 * address family and its round trip when the App measured one.
 */
export function deviceUsingText(t: TFunction, using: DeviceUsing): string {
  let name: string;
  if (using.kind === 'tunnel') {
    name = t(using.relayUp ? 'remoteAccess.direct.usingTunnelRelayUp' : 'remoteAccess.direct.usingTunnel');
  } else {
    name = t(usingNameKeys[using.kind]);
    if ('family' in using) {
      const family = t(using.family === 'ipv4' ? 'remoteAccess.direct.familyIpv4' : 'remoteAccess.direct.familyIpv6');
      name = t('remoteAccess.direct.withDetail', { path: name, detail: family });
    }
    if (using.rttMs !== null) {
      const rtt = t('remoteAccess.direct.legMs', { ms: using.rttMs });
      name = t('remoteAccess.direct.withDetail', { path: name, detail: rtt });
    }
  }
  return t('remoteAccess.direct.using', { path: name });
}

/**
 * The tag beside the instance name: `direct` only while this page's stream is
 * on the direct ingress and the App's path is an IPv4 or IPv6 address, `relay`
 * while it is on the direct ingress through the relay, otherwise none (a
 * browser, the public origin, a path the App does not name, or an App that
 * does not keep its path current: `appReportsPath`, `direct` version 3).
 */
export function directTagKind(
  pageVia: LiveStreamVia,
  appPath: NativeDirectPathKind | null,
  appReportsPath: boolean,
): 'direct' | 'relay' | null {
  if (!appReportsPath || pageVia !== 'direct') return null;
  if (appPath === 'ipv4' || appPath === 'ipv6') return 'direct';
  return appPath === 'relay' ? 'relay' : null;
}

/**
 * The last new connection's race, for the Settings line: each side's time to
 * be ready in ms, `none` when it was not part of the race, or what became of
 * it. Null when the App recorded none.
 */
export function lastConnectionLegs(
  snapshot: NativeDirectPathSnapshot | null,
): { direct: NativeDirectConnection['direct']; public: NativeDirectConnection['public'] } | null {
  const last = snapshot?.lastConnection;
  return last ? { direct: last.direct, public: last.public } : null;
}
