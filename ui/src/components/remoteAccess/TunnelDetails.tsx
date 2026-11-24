// The Cloudflare tunnel's network path and connectivity diagnostics, as Settings · Devices ·
// 「连接方式」 shows them behind 「连接诊断与网络路径」. Props only: the page reads the status and
// runs the diagnostics.
import { Activity, ArrowRight, ChevronDown, Cloud, MapPin, Server } from 'lucide-react';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';

import type { CloudflareEdgeLocation, RemoteAccessStatus, TunnelConnectivityDiagnostics } from '@/context/ApiContext';
import type { TranslationKey } from '@/i18n/types';
import type { DirectMeasurement, MeasureLeg } from '@/lib/nativeDirectMeasure';
import { getTunnelRequestPathDisplayState } from '@/lib/tunnelQuality';
import { timeParts } from '../devices/format';
import { KeptTail } from '../ui/kept-tail';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';

const formatLatency = (milliseconds: number) => (
  milliseconds >= 1000
    ? `${(milliseconds / 1000).toFixed(milliseconds >= 10_000 ? 0 : 1)} s`
    : `${Math.round(milliseconds)} ms`
);

const routeKeys = {
  same_metro: 'remoteAccess.networkRouteSameMetro',
  same_country: 'remoteAccess.networkRouteSameCountry',
  cross_country: 'remoteAccess.networkRouteCrossCountry',
  unknown: 'remoteAccess.networkRouteUnknown',
} as const satisfies Record<string, TranslationKey>;
const routeVariant = { same_metro: 'success', same_country: 'info', cross_country: 'warning', unknown: 'secondary' } as const;

type DiagnosticKey = 'dns' | 'quic' | 'http2';
const diagnosticKeys = {
  dns: 'remoteAccess.diagnosticDns',
  quic: 'remoteAccess.diagnosticQuic',
  http2: 'remoteAccess.diagnosticHttp2',
} as const satisfies Record<DiagnosticKey, TranslationKey>;

function NetworkPath({ status, now }: { status: RemoteAccessStatus; now: number }) {
  const { t } = useTranslation();
  const networkPath = status.network_path;
  if (!status.running || !networkPath) return <p className="text-[12px] text-muted">{t('devices.connection.noNetworkPath')}</p>;
  const place = (location: CloudflareEdgeLocation) => (
    location.location ? t('devices.connection.edgePlace', { place: location.location, colo: location.colo }) : location.colo
  );
  const quality = status.tunnel_quality;
  const fresh = quality ? now - Date.parse(quality.sampled_at) <= 150_000 : false;
  const requestPath = quality?.request_path;
  const requestState = getTunnelRequestPathDisplayState(requestPath);
  const requestLatency = requestState === 'latency' ? requestPath?.latency_ms : null;
  const effective = quality?.transport?.effective || quality?.protocol || 'unknown';
  const protocolLabel = effective === 'http2' ? 'HTTP/2' : effective === 'quic' ? 'QUIC' : t('remoteAccess.protocolUnknown');
  const configured = quality?.transport?.configured || status.transport_protocol;
  const protocol = configured === 'auto' && effective !== 'unknown' ? t('remoteAccess.protocolAutomatic', { protocol: protocolLabel }) : protocolLabel;
  const connections = quality
    ? quality.ha_connections >= 4
      ? t('remoteAccess.connectorConnectionsHealthy')
      : quality.ha_connections > 0
        ? t('remoteAccess.connectorConnectionsPartial', { connections: quality.ha_connections })
        : t('remoteAccess.connectorConnectionsUnavailable')
    : '';
  const locations = networkPath.connector.locations || [];
  const metros = locations.filter((location, index) => locations.findIndex((each) => each.colo === location.colo) === index);
  const connector = metros[0]
    ? metros.length > 1
      ? t('remoteAccess.networkAdditionalLocations', { location: place(metros[0]), count: metros.length - 1 })
      : place(metros[0])
    : t('remoteAccess.networkLocationUnavailable');
  const client = networkPath.client_ingress
    ? place(networkPath.client_ingress)
    : networkPath.client_access === 'remote' ? t('remoteAccess.networkIngressUnavailable') : t('remoteAccess.networkLocalBrowser');
  const assessment = networkPath.route.assessment in routeKeys ? networkPath.route.assessment as keyof typeof routeKeys : 'unknown';
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-[13px] font-semibold text-foreground">{t('remoteAccess.networkPath')}</h3>
        <Badge variant={routeVariant[assessment]}>{t(routeKeys[assessment])}</Badge>
      </div>
      <div className="mt-3 grid border-y border-border/70 md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto_minmax(0,1fr)] md:items-stretch">
        <div className="min-w-0 py-3 md:pr-4">
          <div className="flex items-center gap-1.5 text-[11px] text-muted"><MapPin className="size-3.5 shrink-0" />{t('remoteAccess.networkBrowserIngress')}</div>
          <div className="mt-1 break-words text-[12px] font-medium text-foreground">{client}</div>
        </div>
        <ArrowRight className="my-auto hidden size-4 text-muted md:block" />
        <div className="min-w-0 border-t border-border/70 py-3 md:border-t-0 md:px-4">
          <div className="flex items-center gap-1.5 text-[11px] text-muted"><Cloud className="size-3.5 shrink-0" />{t('remoteAccess.networkProvider')}</div>
          <div className="mt-1 text-[12px] font-medium text-foreground">{t('devices.connection.provider', { name: networkPath.provider, asn: networkPath.asn })}</div>
        </div>
        <ArrowRight className="my-auto hidden size-4 text-muted md:block" />
        <div className="min-w-0 border-t border-border/70 py-3 md:border-t-0 md:pl-4">
          <div className="flex items-center gap-1.5 text-[11px] text-muted"><Server className="size-3.5 shrink-0" />{t('remoteAccess.networkConnectorEdges')}</div>
          <div className="mt-1 break-words text-[12px] font-medium text-foreground">{connector}</div>
        </div>
      </div>
      <details className="group mt-2 text-[11px]">
        <summary className="flex w-fit cursor-pointer list-none items-center gap-1 py-1 text-muted hover:text-foreground">
          <ChevronDown className="size-3.5 transition-transform group-open:rotate-180" />
          {t('remoteAccess.networkTechnicalDetails')}
        </summary>
        <div className="mt-1 grid gap-3 border-t border-border/60 pt-3 sm:grid-cols-2">
          <div className="min-w-0">
            <div className="font-medium text-muted">{t('remoteAccess.networkEdgeNodes')}</div>
            <div className="mt-1 space-y-1 font-mono text-foreground/85">
              {locations.length > 0
                ? locations.map((location) => <div key={location.id} className="break-all">{t('devices.connection.edgeNode', { id: location.id, place: place(location) })}</div>)
                : <div>{t('remoteAccess.networkLocationUnavailable')}</div>}
            </div>
          </div>
          <div className="min-w-0">
            <div className="font-medium text-muted">{t('remoteAccess.networkAnycastIps')}</div>
            <div className="mt-1 space-y-1 font-mono text-foreground/85">
              {networkPath.connector.edge_ips.length > 0
                ? networkPath.connector.edge_ips.map((address) => <div key={address} className="break-all">{address}</div>)
                : <div>{t('remoteAccess.networkIpUnavailable')}</div>}
            </div>
          </div>
          {fresh && quality && (
            <div className="min-w-0 border-t border-border/60 pt-3 sm:col-span-2">
              <div className="font-medium text-muted">{t('remoteAccess.quality')}</div>
              <div className="mt-1 space-y-1 text-foreground/85">
                <div className="break-words">
                  {quality.rtt_ms
                    ? t('remoteAccess.connectorPathWithLatency', { protocol, connections, rtt: formatLatency(quality.rtt_ms.median) })
                    : t('remoteAccess.connectorPath', { protocol, connections })}
                </div>
                {requestState === 'unavailable' ? (
                  <div className="break-words font-medium text-destructive-ink">
                    {t('remoteAccess.requestPathUnavailable', { success: requestPath?.success_count || 0, count: requestPath?.sample_count || 0 })}
                  </div>
                ) : requestLatency ? (
                  <div className="break-words font-mono text-foreground/80">
                    {t('remoteAccess.requestPath', {
                      p95: formatLatency(requestLatency.p95),
                      p99: formatLatency(requestLatency.p99),
                      slow: Math.round((requestPath?.slow_request_rate.over_1000_ms || 0) * 100),
                    })}
                  </div>
                ) : requestPath ? <div>{t('remoteAccess.requestPathMeasuring', { count: requestPath.sample_count })}</div> : null}
              </div>
            </div>
          )}
        </div>
      </details>
    </div>
  );
}

/** Direct, for advanced users and debugging: its certificate. */
export type DirectDetails = {
  /** The certificate's expiry while it is valid; null without a valid one. */
  certificate: Date | null;
};

/** 「测速」: this device's shell measures small round trips over the path it uses now and over the
 *  Cloudflare tunnel (`direct.measure()`). Nothing is shown before a measurement has run. */
export type MeasureDetails = {
  running?: boolean;
  result?: DirectMeasurement;
  /** `busy`: another measurement is running. `failed`: none could be read. */
  error?: 'busy' | 'failed';
};

function DirectRows({ direct }: { direct: DirectDetails }) {
  const { t, i18n } = useTranslation();
  const rows: Array<[string, string]> = [
    [
      t('devices.connection.directCertificate'),
      direct.certificate
        ? t('devices.connection.validUntil', { date: new Intl.DateTimeFormat(i18n.language, { year: 'numeric', month: 'long', day: 'numeric' }).format(direct.certificate) })
        : t('devices.connection.noCertificate'),
    ],
  ];
  return (
    <div className="border-t border-border/70 pt-4" data-testid="direct-details">
      <h3 className="text-[13px] font-semibold text-foreground">{t('remoteAccess.direct.label')}</h3>
      <dl className="mt-2 grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-6 gap-y-1.5 text-[12px]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted">{label}</dt>
            <dd className="tabular-nums text-foreground">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

const atKeys = {
  today: 'devices.connection.measure.atToday',
  yesterday: 'devices.connection.measure.atYesterday',
  other: 'devices.connection.measure.atDated',
} as const satisfies Record<NonNullable<ReturnType<typeof timeParts>>['day'], TranslationKey>;

/** A measured path by its shipped name, with its address family or the relay's region. */
function legName(t: TFunction, leg: MeasureLeg): string {
  if (leg.kind === 'tunnel') return t('remoteAccess.direct.usingTunnel');
  if (leg.kind === 'relay') {
    const relay = t('remoteAccess.direct.usingRelay');
    return leg.relayRegion ? t('remoteAccess.direct.withDetail', { path: relay, detail: leg.relayRegion }) : relay;
  }
  const name = t(leg.scope === 'local' ? 'remoteAccess.direct.usingLan' : leg.scope === 'global' ? 'remoteAccess.direct.usingPunched' : 'remoteAccess.direct.usingDirect');
  return t('remoteAccess.direct.withDetail', { path: name, detail: t(leg.kind === 'ipv4' ? 'remoteAccess.direct.familyIpv4' : 'remoteAccess.direct.familyIpv6') });
}

/** Median and worst over the completed samples; what became of the rest. */
function legValue(t: TFunction, leg: MeasureLeg, samples: number): string {
  if (leg.ok === 0 || leg.medianMs === null || leg.maxMs === null) {
    return t(leg.error === 'timeout' ? 'devices.connection.measure.timedOut' : 'devices.connection.measure.failed');
  }
  const value = t('devices.connection.measure.value', { median: formatLatency(leg.medianMs), max: formatLatency(leg.maxMs) });
  return leg.ok < samples ? t('devices.connection.measure.partial', { value, ok: leg.ok, samples }) : value;
}

function MeasureRows({ measure, onMeasure }: { measure: MeasureDetails; onMeasure?: () => void }) {
  const { t, i18n } = useTranslation();
  const { result } = measure;
  const at = result ? timeParts(new Date(result.measuredAt).toISOString(), i18n.language) : null;
  const rows: Array<[string, string]> = result ? [
    result.current
      ? [legName(t, result.current), legValue(t, result.current, result.samples)]
      : [t('devices.connection.measure.directOrRelay'), t('devices.connection.measure.noCurrent')],
    [t('remoteAccess.direct.usingTunnel'), result.tunnel ? legValue(t, result.tunnel, result.samples) : t('devices.connection.measure.notMeasured')],
  ] : [];
  return (
    <div className="border-t border-border/70 pt-4" data-testid="direct-measure">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-[13px] font-semibold text-foreground">{t('devices.connection.measure.title')}</h3>
        <Button type="button" variant="secondary" size="xs" disabled={measure.running} onClick={onMeasure}>
          <Activity className="size-3.5" />
          {measure.running ? t('devices.connection.measure.running') : t('devices.connection.measure.run')}
        </Button>
      </div>
      <p className="mt-1 text-[12px] leading-relaxed text-muted"><KeptTail text={t('devices.connection.measure.lead')} /></p>
      {measure.error && (
        <p role="alert" className="mt-2 text-[12px] text-gold-ink">
          {t(measure.error === 'busy' ? 'devices.connection.measure.busy' : 'devices.connection.measure.unreadable')}
        </p>
      )}
      {result && (
        <>
          {/* On a phone each path's name sits above its times, so the times keep one line. */}
          <dl className="mt-3 grid gap-y-1.5 text-[12px] sm:grid-cols-[minmax(0,auto)_minmax(0,1fr)] sm:gap-x-6">
            {rows.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="text-muted">{label}</dt>
                <dd className="mb-1 tabular-nums text-foreground sm:mb-0">{value}</dd>
              </div>
            ))}
          </dl>
          {at && <p className="mt-2 text-[11px] text-muted">{t(atKeys[at.day], { date: at.date, time: at.time })}</p>}
        </>
      )}
    </div>
  );
}

export type TunnelDetailsProps = {
  status: RemoteAccessStatus | null;
  diagnostics: TunnelConnectivityDiagnostics | null;
  running?: boolean;
  /** The diagnostics did not finish: why, in the page's words. */
  error?: string;
  /** When the page read the status, for the freshness of what it shows. */
  now: number;
  /** Present only while this Avibe has direct turned on. */
  direct?: DirectDetails;
  /** Present only where this device's shell can measure (`directMeasure`). */
  measure?: MeasureDetails;
  onRun?: () => void;
  onMeasure?: () => void;
};

export function TunnelDetails({ status, diagnostics, running, error, now, direct, measure, onRun, onMeasure }: TunnelDetailsProps) {
  const { t } = useTranslation();
  const rows: DiagnosticKey[] = diagnostics ? ['dns', 'quic', 'http2'] : [];
  const inactiveQuic = (key: DiagnosticKey) => key === 'quic' && diagnostics?.quic.status === 'unknown' && diagnostics.effective_protocol === 'http2';
  return (
    <div className="flex flex-col gap-5">
      {status ? <NetworkPath status={status} now={now} /> : <p className="text-[12px] text-muted">{t('devices.connection.noNetworkPath')}</p>}
      {direct && <DirectRows direct={direct} />}
      {measure && <MeasureRows measure={measure} onMeasure={onMeasure} />}
      <div className="border-t border-border/70 pt-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <h3 className="text-[13px] font-semibold text-foreground">{t('remoteAccess.diagnostics')}</h3>
            {diagnostics?.cloudflared_version && (
              <span className="font-mono text-[10px] text-muted">{t('remoteAccess.diagnosticVersion', { version: diagnostics.cloudflared_version })}</span>
            )}
          </div>
          <Button type="button" variant="secondary" size="xs" disabled={running} onClick={onRun}>
            <Activity className="size-3.5" />
            {running ? t('remoteAccess.runningDiagnostics') : t('remoteAccess.runDiagnostics')}
          </Button>
        </div>
        {error && <p role="alert" className="mt-2 text-[12px] text-gold-ink">{error}</p>}
        {rows.length > 0 && diagnostics && (
          <div className="mt-3 grid border-y border-border/70 sm:grid-cols-3">
            {rows.map((key, index) => {
              const state = diagnostics[key].status;
              return (
                <div key={key} className={`flex min-h-11 items-center justify-between gap-2 px-3 py-2 ${index > 0 ? 'border-t border-border/70 sm:border-l sm:border-t-0' : ''}`}>
                  <span className="text-[11px] text-muted">{t(diagnosticKeys[key])}</span>
                  <Badge
                    title={inactiveQuic(key) ? t('remoteAccess.diagnosticQuicInactiveHelp', { protocol: 'HTTP/2' }) : undefined}
                    variant={state === 'available' ? 'success' : state === 'unavailable' ? 'destructive' : 'secondary'}
                  >
                    {inactiveQuic(key)
                      ? t('remoteAccess.diagnosticQuicInactive', { protocol: 'HTTP/2' })
                      : t(state === 'available' ? 'remoteAccess.diagnosticAvailable' : state === 'unavailable' ? 'remoteAccess.diagnosticUnavailable' : 'remoteAccess.diagnosticUnknown')}
                  </Badge>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
