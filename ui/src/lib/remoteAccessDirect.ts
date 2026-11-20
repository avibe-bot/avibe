import type { RemoteAccessStatus } from '../context/ApiContext';
import type { TranslationKey } from '../i18n/types';
import type { LiveStreamVia } from './liveStreamVia';
import type { NativeDirectPathSnapshot } from './native';

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

/** Null while direct is turned off. */
export function remoteAccessDirectLines(
  direct: RemoteAccessStatus['direct'],
  pageVia: LiveStreamVia,
  now: number,
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
  const pageKey: TranslationKey | null = pageVia === 'direct'
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

/**
 * In the App: whether its own direct path is up, and whether this page's live
 * stream uses it (`使用中`) or not (`已开启`), with the kind of path it runs
 * on when the App can tell. Null in a browser, and while the App's path is not
 * up (`preferred` is not `direct`).
 */
export function deviceDirectLine(
  snapshot: NativeDirectPathSnapshot | null,
  pageVia: LiveStreamVia,
): { stateKey: TranslationKey; pathKey: TranslationKey | null } | null {
  if (!snapshot || snapshot.preferred !== 'direct') return null;
  const pathKey: TranslationKey | null = snapshot.path === 'relay'
    ? 'remoteAccess.direct.pathRelay'
    : snapshot.path === 'ipv4'
      ? 'remoteAccess.direct.pathIpv4'
      : snapshot.path === 'ipv6'
        ? 'remoteAccess.direct.pathIpv6'
        : null;
  return {
    stateKey: pageVia === 'direct' ? 'remoteAccess.direct.deviceInUse' : 'remoteAccess.direct.deviceOn',
    pathKey,
  };
}
