import React, { useCallback, useEffect, useState } from 'react';
import {
  Activity,
  ArrowRight,
  CheckCircle2,
  ChevronDown,
  Cloud,
  ExternalLink,
  Link2,
  MapPin,
  Network,
  RefreshCcw,
  Route,
  Save,
  Server,
  Settings2,
} from 'lucide-react';
import { Trans, useTranslation } from 'react-i18next';
import {
  type CloudflareEdgeLocation,
  type RemoteAccessSettings,
  type RemoteAccessStatus,
  type TunnelConnectivityDiagnostics,
  type TunnelNetworkInterface,
  useApi,
} from '../context/ApiContext';
import { useToast } from '../context/ToastContext';
import { getTunnelQualityDisplayState, getTunnelRequestPathDisplayState } from '../lib/tunnelQuality';
import { CompactField } from './settings/SettingsPrimitives';
import { Button } from './ui/button';
import { Badge } from './ui/badge';
import { SegmentedRadio } from './ui/segmented';
import { Select } from './ui/select';
import { Switch } from './ui/switch';

const VIBE_CLOUD_URL = 'https://avibe.bot';
const VIBE_CLOUD_APP_URL = 'https://avibe.bot/app';
const DEFAULT_SETTINGS: RemoteAccessSettings = {
  transport_protocol: 'auto',
  auto_recovery: true,
  optimization_profile: 'balanced',
  edge_ip_version: '4',
  edge_bind_address: '',
};

const formatLatency = (milliseconds: number) => (
  milliseconds >= 1000
    ? `${(milliseconds / 1000).toFixed(milliseconds >= 10_000 ? 0 : 1)} s`
    : `${Math.round(milliseconds)} ms`
);

const formatEdgeLocation = (location: CloudflareEdgeLocation) => (
  location.location ? `${location.location} (${location.colo})` : location.colo
);

export const RemoteAccess: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [loading, setLoading] = useState(true);
  const [pairing, setPairing] = useState(false);
  const [status, setStatus] = useState<RemoteAccessStatus | null>(null);
  const [optimizing, setOptimizing] = useState(false);
  const [pairingKey, setPairingKey] = useState('');
  const [reconfiguring, setReconfiguring] = useState(false);
  const [settingsDraft, setSettingsDraft] = useState<RemoteAccessSettings>(DEFAULT_SETTINGS);
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [networkInterfaces, setNetworkInterfaces] = useState<TunnelNetworkInterface[]>([]);
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagnostics, setDiagnostics] = useState<TunnelConnectivityDiagnostics | null>(null);
  const [actionMessage, setActionMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const describeError = (payload: unknown) => {
    const code = payload && typeof payload === 'object' && 'error' in payload && typeof payload.error === 'string'
      ? payload.error
      : '';
    if (!code) {
      return t('errors.remote_access_unknown');
    }
    return t(`errors.${code}`, { defaultValue: t('errors.remote_access_unknown') });
  };

  const refresh = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const remoteStatus = await api.remoteAccessStatus();
      setStatus(remoteStatus);
      if (remoteStatus.paired) {
        try {
          const result = await api.getRemoteAccessNetworkInterfaces();
          setNetworkInterfaces(result.interfaces || []);
        } catch {
          setNetworkInterfaces([]);
        }
      } else {
        setNetworkInterfaces([]);
      }
    } finally {
      if (!silent) setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    refresh().catch(() => setLoading(false));
    const disconnect = api.connectWorkbenchEvents({
      onRemoteAccessQuality: (quality) => {
        setStatus((current) => current ? { ...current, tunnel_quality: quality } : current);
      },
    });
    const refreshVisible = () => {
      if (document.visibilityState === 'visible') refresh(true).catch(() => undefined);
    };
    const interval = window.setInterval(refreshVisible, 30_000);
    document.addEventListener('visibilitychange', refreshVisible);
    window.addEventListener('focus', refreshVisible);
    return () => {
      disconnect();
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', refreshVisible);
      window.removeEventListener('focus', refreshVisible);
    };
  }, [api, refresh]);

  useEffect(() => {
    if (!settingsDirty && status?.settings) {
      setSettingsDraft(status.settings);
    }
  }, [settingsDirty, status?.settings]);

  const pair = async () => {
    setPairing(true);
    setActionMessage(null);
    try {
      const result = await api.pairVibeCloudRemoteAccess({
        backend_url: VIBE_CLOUD_URL,
        pairing_key: pairingKey.trim(),
        device_name: 'avibe',
      });
      setStatus(result);
      setPairingKey('');
      if (result?.start?.ok === false) {
        const message = describeError(result.start);
        setActionMessage({ type: 'error', text: message });
        showToast(message, 'error');
      } else {
        const message = t('remoteAccess.pairSuccess');
        setReconfiguring(false);
        setActionMessage({ type: 'success', text: message });
        showToast(message, 'success');
      }
      await refresh();
    } catch (error) {
      const message = error instanceof Error ? error.message : t('errors.remote_access_unknown');
      setActionMessage({ type: 'error', text: message });
    } finally {
      setPairing(false);
    }
  };

  const stop = async () => {
    setActionMessage(null);
    try {
      const result = await api.stopRemoteAccess();
      setStatus(result);
      if (result?.ok === false) {
        const message = describeError(result);
        setActionMessage({ type: 'error', text: message });
        showToast(message, 'error');
        return;
      }
      const message = t('remoteAccess.stopSuccess');
      setActionMessage({ type: 'success', text: message });
      showToast(message, 'success');
    } catch (error) {
      const message = error instanceof Error ? error.message : t('errors.remote_access_unknown');
      setActionMessage({ type: 'error', text: message });
    }
  };

  const start = async () => {
    setActionMessage(null);
    try {
      const result = await api.startRemoteAccess();
      setStatus(result);
      if (result?.ok === false) {
        const message = describeError(result);
        setActionMessage({ type: 'error', text: message });
        showToast(message, 'error');
        return;
      }
      const message = t('remoteAccess.startSuccess');
      setActionMessage({ type: 'success', text: message });
      showToast(message, 'success');
      await refresh(true).catch(() => undefined);
    } catch (error) {
      const message = error instanceof Error ? error.message : t('errors.remote_access_unknown');
      setActionMessage({ type: 'error', text: message });
    }
  };

  const optimizeRoute = async () => {
    setOptimizing(true);
    setActionMessage(null);
    try {
      const result = await api.optimizeRemoteAccessRoute();
      setStatus(result);
      const message = t('remoteAccess.optimizeStarted');
      setActionMessage({ type: 'success', text: message });
      showToast(message, 'success');
      await refresh(true).catch(() => undefined);
    } catch (error) {
      const message = error instanceof Error ? error.message : t('errors.remote_access_unknown');
      setActionMessage({ type: 'error', text: message });
    } finally {
      setOptimizing(false);
    }
  };

  const updateSetting = <K extends keyof RemoteAccessSettings>(
    key: K,
    value: RemoteAccessSettings[K],
  ) => {
    setSettingsDraft((current) => ({ ...current, [key]: value }));
    setSettingsDirty(true);
  };

  const saveTunnelSettings = async () => {
    setSavingSettings(true);
    setActionMessage(null);
    try {
      const result = await api.saveRemoteAccessSettings(settingsDraft);
      setStatus(result);
      if (result?.ok === false) {
        const message = describeError(result);
        setActionMessage({ type: 'error', text: message });
        showToast(message, 'error');
        return;
      }
      setSettingsDraft(result.settings || settingsDraft);
      setSettingsDirty(false);
      const message = t('remoteAccess.controlsSaved');
      setActionMessage({ type: 'success', text: message });
      showToast(message, 'success');
      await refresh(true).catch(() => undefined);
    } catch (error) {
      const message = error instanceof Error ? error.message : t('errors.remote_access_unknown');
      setActionMessage({ type: 'error', text: message });
      showToast(message, 'error');
    } finally {
      setSavingSettings(false);
    }
  };

  const runDiagnostics = async () => {
    setDiagnosing(true);
    setActionMessage(null);
    try {
      const result = await api.diagnoseRemoteAccess();
      if (result?.ok === false) {
        const message = describeError(result);
        setActionMessage({ type: 'error', text: message });
        showToast(message, 'error');
        return;
      }
      setDiagnostics(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : t('errors.remote_access_diagnostics_failed');
      setActionMessage({ type: 'error', text: message });
    } finally {
      setDiagnosing(false);
    }
  };

  const publicUrl = status?.public_url;
  const paired = Boolean(status?.paired);
  const running = Boolean(status?.running);
  const showPairingForm = !paired || reconfiguring;
  const connectorState = status?.pid_state === 'unknown'
    ? t('remoteAccess.stateNeedsAttention')
    : running
      ? t('common.running')
      : t('common.stopped');
  const quality = status?.tunnel_quality;
  const qualityFresh = quality
    ? Date.now() - Date.parse(quality.sampled_at) <= 150_000
    : false;
  const qualityGrade = getTunnelQualityDisplayState(quality, qualityFresh);
  const qualityVariant = qualityGrade === 'good'
    ? 'success'
    : qualityGrade === 'fair'
      ? 'info'
      : qualityGrade === 'poor' || qualityGrade === 'recovering'
        ? 'warning'
        : qualityGrade === 'critical' || qualityGrade === 'degraded'
          ? 'destructive'
          : 'secondary';
  const qualityLabel = t(`remoteAccess.quality${qualityGrade.charAt(0).toUpperCase()}${qualityGrade.slice(1)}`);
  const requestPath = quality?.request_path;
  const requestPathDisplayState = getTunnelRequestPathDisplayState(requestPath);
  const requestPathUnavailable = requestPathDisplayState === 'unavailable';
  const requestLatency = requestPathDisplayState === 'latency' ? requestPath?.latency_ms : null;
  const effectiveProtocol = quality?.transport?.effective || quality?.protocol || 'unknown';
  const protocolLabel = effectiveProtocol === 'http2'
    ? 'HTTP/2'
    : effectiveProtocol === 'quic'
      ? 'QUIC'
      : t('remoteAccess.protocolUnknown');
  const configuredProtocol = quality?.transport?.configured || status?.transport_protocol;
  const protocolDisplay = configuredProtocol === 'auto' && effectiveProtocol !== 'unknown'
    ? t('remoteAccess.protocolAutomatic', { protocol: protocolLabel })
    : protocolLabel;
  const connectorConnectionLabel = quality
    ? quality.ha_connections >= 4
      ? t('remoteAccess.connectorConnectionsHealthy')
      : quality.ha_connections > 0
        ? t('remoteAccess.connectorConnectionsPartial', { connections: quality.ha_connections })
        : t('remoteAccess.connectorConnectionsUnavailable')
    : '';
  const connectorPathLabel = quality
    ? quality.rtt_ms
      ? t('remoteAccess.connectorPathWithLatency', {
          protocol: protocolDisplay,
          connections: connectorConnectionLabel,
          rtt: formatLatency(quality.rtt_ms.median),
        })
      : t('remoteAccess.connectorPath', {
          protocol: protocolDisplay,
          connections: connectorConnectionLabel,
        })
    : '';
  const networkPath = status?.network_path;
  const connectorLocations = networkPath?.connector.locations || [];
  const connectorMetros = connectorLocations.filter((location, index) => (
    connectorLocations.findIndex((candidate) => candidate.colo === location.colo) === index
  ));
  const primaryConnectorLocation = connectorMetros[0];
  const connectorLocationSummary = primaryConnectorLocation
    ? connectorMetros.length > 1
      ? t('remoteAccess.networkAdditionalLocations', {
          location: formatEdgeLocation(primaryConnectorLocation),
          count: connectorMetros.length - 1,
        })
      : formatEdgeLocation(primaryConnectorLocation)
    : t('remoteAccess.networkLocationUnavailable');
  const clientLocationSummary = networkPath?.client_ingress
    ? formatEdgeLocation(networkPath.client_ingress)
    : networkPath?.client_access === 'remote'
      ? t('remoteAccess.networkIngressUnavailable')
      : t('remoteAccess.networkLocalBrowser');
  const routeAssessment = networkPath?.route.assessment || 'unknown';
  const routeVariant = routeAssessment === 'same_metro'
    ? 'success'
    : routeAssessment === 'same_country'
      ? 'info'
      : routeAssessment === 'cross_country'
        ? 'warning'
        : 'secondary';
  const routeLabel = t(`remoteAccess.networkRoute${
    routeAssessment === 'same_metro'
      ? 'SameMetro'
      : routeAssessment === 'same_country'
        ? 'SameCountry'
        : routeAssessment === 'cross_country'
          ? 'CrossCountry'
          : 'Unknown'
  }`);
  const controlsDisabled = savingSettings || optimizing || (qualityFresh && quality?.state === 'recovering');
  const diagnosticRows = diagnostics ? [
    { key: 'dns', label: t('remoteAccess.diagnosticDns'), status: diagnostics.dns.status },
    { key: 'quic', label: t('remoteAccess.diagnosticQuic'), status: diagnostics.quic.status },
    { key: 'http2', label: t('remoteAccess.diagnosticHttp2'), status: diagnostics.http2.status },
  ] as const : [];
  const diagnosticLabel = (
    diagnosticKey: 'dns' | 'quic' | 'http2',
    diagnosticStatus: 'available' | 'unavailable' | 'unknown',
  ) => {
    if (diagnosticKey === 'quic' && diagnosticStatus === 'unknown' && diagnostics?.effective_protocol === 'http2') {
      return t('remoteAccess.diagnosticQuicInactive', { protocol: 'HTTP/2' });
    }
    return diagnosticStatus === 'available'
      ? t('remoteAccess.diagnosticAvailable')
      : diagnosticStatus === 'unavailable'
        ? t('remoteAccess.diagnosticUnavailable')
        : t('remoteAccess.diagnosticUnknown');
  };
  const diagnosticTitle = (
    diagnosticKey: 'dns' | 'quic' | 'http2',
    diagnosticStatus: 'available' | 'unavailable' | 'unknown',
  ) => (
    diagnosticKey === 'quic' && diagnosticStatus === 'unknown' && diagnostics?.effective_protocol === 'http2'
      ? t('remoteAccess.diagnosticQuicInactiveHelp', { protocol: 'HTTP/2' })
      : undefined
  );

  return (
    <section
      id="remote-access"
      className="scroll-mt-24 overflow-hidden rounded-xl border border-cyan/45 bg-cyan/[0.06] shadow-[0_0_40px_-10px_rgba(63,224,229,0.45)]"
    >
      <div className="flex items-start justify-between gap-4 border-b border-cyan/20 bg-cyan/[0.07] px-5 py-4">
        <div className="min-w-0 space-y-2">
          <h2 className="inline-flex items-center gap-2 text-[15px] font-semibold text-foreground">
            <span className="flex size-8 shrink-0 items-center justify-center rounded-full border border-cyan/30 bg-cyan/[0.12] text-cyan-ink">
              <Cloud className="size-4" strokeWidth={2.25} />
            </span>
            {t('remoteAccess.title')}
          </h2>
          <p className="max-w-2xl text-[12px] leading-relaxed text-muted">{t('remoteAccess.subtitleWithLink')}</p>
          <ol className="ml-4 list-decimal space-y-1 text-[12px] leading-relaxed text-muted">
            <li>
              <Trans
                i18nKey="remoteAccess.flowStep1"
                components={{
                  console: (
                    <a
                      href={VIBE_CLOUD_APP_URL}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-0.5 font-medium text-cyan-ink hover:underline"
                    />
                  ),
                }}
              />
            </li>
            <li>{t('remoteAccess.flowStep2')}</li>
            <li>{t('remoteAccess.flowStep3')}</li>
          </ol>
        </div>
        <Button
          variant="secondary"
          size="xs"
          className="shrink-0"
          onClick={() => refresh()}
          type="button"
        >
          <RefreshCcw className="size-3.5" />
          {t('common.refresh')}
        </Button>
      </div>

      <div className="grid border-b border-border sm:grid-cols-2 lg:grid-cols-4">
        <div className="border-b border-border px-5 py-3.5 sm:border-r lg:border-b-0">
          <div className="text-[12px] text-muted">{t('remoteAccess.paired')}</div>
          <div className="mt-1">
            {paired ? (
              <Badge variant="success">{t('common.enabled')}</Badge>
            ) : (
              <Badge variant="secondary">{t('common.disabled')}</Badge>
            )}
          </div>
        </div>
        <div className="border-b border-border px-5 py-3.5 lg:border-b-0 lg:border-r">
          <div className="text-[12px] text-muted">{t('remoteAccess.connector')}</div>
          <div className="mt-1 text-[13px] font-medium text-foreground">{loading ? t('common.loading') : connectorState}</div>
        </div>
        <div className="border-b border-border px-5 py-3.5 sm:border-b-0 sm:border-r">
          <div className="text-[12px] text-muted">{t('remoteAccess.vibeCloudService')}</div>
          <a className="mt-1 inline-flex text-[13px] font-medium text-cyan-ink" href={VIBE_CLOUD_URL} target="_blank" rel="noreferrer">
            avibe.bot
            <ExternalLink className="ml-1 size-3.5" />
          </a>
        </div>
        <div className="px-5 py-3.5">
          <div className="text-[12px] text-muted">{t('remoteAccess.quality')}</div>
          <div className="mt-1 flex min-h-5 items-center gap-2">
            <Badge variant={qualityVariant}>{qualityLabel}</Badge>
            {qualityFresh && (requestLatency || quality?.rtt_ms) && (
              <span className="text-[11px] font-medium tabular-nums text-foreground">
                {t('remoteAccess.qualityLatency', {
                  latency: requestLatency
                    ? formatLatency(requestLatency.p95)
                    : formatLatency(quality!.rtt_ms!.median),
                })}
              </span>
            )}
          </div>
        </div>
      </div>

      {running && networkPath && (
        <div className="border-b border-border px-5 py-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <Network className="size-4 shrink-0 text-cyan-ink" />
              <h3 className="text-[13px] font-semibold text-foreground">{t('remoteAccess.networkPath')}</h3>
            </div>
            <Badge variant={routeVariant}>{routeLabel}</Badge>
          </div>

          <div className="mt-3 grid border-y border-border/70 md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto_minmax(0,1fr)] md:items-stretch">
            <div className="min-w-0 py-3 md:pr-4">
              <div className="flex items-center gap-1.5 text-[11px] text-muted">
                <MapPin className="size-3.5 shrink-0" />
                {t('remoteAccess.networkBrowserIngress')}
              </div>
              <div className="mt-1 break-words text-[12px] font-medium text-foreground">{clientLocationSummary}</div>
            </div>
            <ArrowRight className="my-auto hidden size-4 text-muted md:block" />
            <div className="min-w-0 border-t border-border/70 py-3 md:border-l-0 md:border-t-0 md:px-4">
              <div className="flex items-center gap-1.5 text-[11px] text-muted">
                <Cloud className="size-3.5 shrink-0" />
                {t('remoteAccess.networkProvider')}
              </div>
              <div className="mt-1 text-[12px] font-medium text-foreground">
                {networkPath.provider} <span className="font-mono text-[11px] text-muted">AS{networkPath.asn}</span>
              </div>
            </div>
            <ArrowRight className="my-auto hidden size-4 text-muted md:block" />
            <div className="min-w-0 border-t border-border/70 py-3 md:border-l-0 md:border-t-0 md:pl-4">
              <div className="flex items-center gap-1.5 text-[11px] text-muted">
                <Server className="size-3.5 shrink-0" />
                {t('remoteAccess.networkConnectorEdges')}
              </div>
              <div className="mt-1 break-words text-[12px] font-medium text-foreground">{connectorLocationSummary}</div>
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
                  {connectorLocations.length > 0 ? connectorLocations.map((location) => (
                    <div key={location.id} className="break-all">
                      {location.id} · {formatEdgeLocation(location)}
                    </div>
                  )) : <div>{t('remoteAccess.networkLocationUnavailable')}</div>}
                </div>
              </div>
              <div className="min-w-0">
                <div className="font-medium text-muted">{t('remoteAccess.networkAnycastIps')}</div>
                <div className="mt-1 space-y-1 font-mono text-foreground/85">
                  {networkPath.connector.edge_ips.length > 0 ? networkPath.connector.edge_ips.map((address) => (
                    <div key={address} className="break-all">{address}</div>
                  )) : <div>{t('remoteAccess.networkIpUnavailable')}</div>}
                </div>
              </div>
              {qualityFresh && quality && (
                <div className="min-w-0 border-t border-border/60 pt-3 sm:col-span-2">
                  <div className="font-medium text-muted">{t('remoteAccess.quality')}</div>
                  <div className="mt-1 space-y-1 text-foreground/85">
                    <div className="break-words" title={quality.edge_locations?.join(', ')}>
                      {connectorPathLabel}
                    </div>
                    {requestPathUnavailable ? (
                      <div className="break-words font-medium text-destructive-ink">
                        {t('remoteAccess.requestPathUnavailable', {
                          success: requestPath?.success_count || 0,
                          count: requestPath?.sample_count || 0,
                        })}
                      </div>
                    ) : requestLatency ? (
                      <div className="break-words font-mono text-foreground/80">
                        {t('remoteAccess.requestPath', {
                          p95: formatLatency(requestLatency.p95),
                          p99: formatLatency(requestLatency.p99),
                          slow: Math.round((requestPath?.slow_request_rate.over_1000_ms || 0) * 100),
                        })}
                      </div>
                    ) : requestPath ? (
                      <div>{t('remoteAccess.requestPathMeasuring', { count: requestPath.sample_count })}</div>
                    ) : null}
                  </div>
                </div>
              )}
            </div>
          </details>
        </div>
      )}

      {paired && !showPairingForm && (
        <details className="group border-b border-border">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-5 py-4 transition-colors hover:bg-surface/40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-cyan/60">
            <div className="flex items-center gap-2">
              <Settings2 className="size-4 text-cyan-ink" />
              <h3 className="text-[13px] font-semibold text-foreground">{t('remoteAccess.controls')}</h3>
            </div>
            <ChevronDown className="size-4 shrink-0 text-muted transition-transform group-open:rotate-180" />
          </summary>

          <div className="px-5 pb-4">
            <div className="grid gap-x-5 gap-y-4 md:grid-cols-2">
              <div className="min-w-0 space-y-1.5">
                <span className="flex min-h-5 items-center justify-between gap-2 text-[12px] font-medium text-foreground">
                  {t('remoteAccess.protocol')}
                  {effectiveProtocol !== 'unknown' && (
                    <Badge variant="secondary">{protocolLabel}</Badge>
                  )}
                </span>
                <SegmentedRadio
                  value={settingsDraft.transport_protocol}
                  onChange={(value) => updateSetting('transport_protocol', value)}
                  ariaLabel={t('remoteAccess.protocol')}
                  disabled={controlsDisabled}
                  tone="cyan"
                  options={[
                    { id: 'auto', label: t('remoteAccess.protocolAuto') },
                    { id: 'quic', label: 'QUIC' },
                    { id: 'http2', label: 'HTTP/2' },
                  ]}
                />
              </div>

              <div className="min-w-0 space-y-1.5">
                <span className="block min-h-5 text-[12px] font-medium text-foreground">{t('remoteAccess.autoRecovery')}</span>
                <div className="flex h-9 items-center justify-between border-y border-border/70 px-1">
                  <Activity className="size-4 text-muted" />
                  <Switch
                    size="sm"
                    checked={settingsDraft.auto_recovery}
                    onCheckedChange={(value) => updateSetting('auto_recovery', value)}
                    label={t('remoteAccess.autoRecovery')}
                    disabled={controlsDisabled}
                  />
                </div>
              </div>

              <div className="min-w-0 space-y-1.5">
                <span className="block min-h-5 text-[12px] font-medium text-foreground">{t('remoteAccess.optimizationProfile')}</span>
                <SegmentedRadio
                  value={settingsDraft.optimization_profile}
                  onChange={(value) => updateSetting('optimization_profile', value)}
                  ariaLabel={t('remoteAccess.optimizationProfile')}
                  disabled={controlsDisabled}
                  options={[
                    { id: 'stable', label: t('remoteAccess.profileStable') },
                    { id: 'balanced', label: t('remoteAccess.profileBalanced') },
                    { id: 'low_latency', label: t('remoteAccess.profileLowLatency') },
                  ]}
                />
              </div>

              <div className="min-w-0 space-y-1.5">
                <span className="block min-h-5 text-[12px] font-medium text-foreground">{t('remoteAccess.ipFamily')}</span>
                <SegmentedRadio
                  value={settingsDraft.edge_ip_version}
                  onChange={(value) => updateSetting('edge_ip_version', value)}
                  ariaLabel={t('remoteAccess.ipFamily')}
                  disabled={controlsDisabled}
                  options={[
                    { id: '4', label: t('remoteAccess.ipV4') },
                    { id: 'auto', label: t('remoteAccess.ipAuto') },
                    { id: '6', label: t('remoteAccess.ipV6') },
                  ]}
                />
              </div>

              <label className="min-w-0 space-y-1.5 md:col-span-2">
                <span className="block text-[12px] font-medium text-foreground">{t('remoteAccess.outboundInterface')}</span>
                <Select
                  value={settingsDraft.edge_bind_address}
                  onChange={(event) => updateSetting('edge_bind_address', event.target.value)}
                  disabled={controlsDisabled}
                  className="font-mono text-[12px]"
                >
                  <option value="">{t('remoteAccess.outboundSystem')}</option>
                  {networkInterfaces.map((networkInterface) => (
                    <option key={networkInterface.id} value={networkInterface.address}>
                      {networkInterface.name} · {networkInterface.address}
                    </option>
                  ))}
                </Select>
              </label>
            </div>

            <div className="mt-4 flex justify-end">
              <Button
                type="button"
                variant="secondary"
                size="xs"
                disabled={!settingsDirty || controlsDisabled}
                onClick={saveTunnelSettings}
              >
                <Save className="size-3.5" />
                {savingSettings ? t('remoteAccess.savingControls') : t('remoteAccess.saveControls')}
              </Button>
            </div>

            <div className="mt-4 border-t border-border/70 pt-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Activity className="size-4 text-cyan-ink" />
                  <span className="text-[12px] font-medium text-foreground">{t('remoteAccess.diagnostics')}</span>
                  {diagnostics?.cloudflared_version && (
                    <span className="font-mono text-[10px] text-muted">
                      {t('remoteAccess.diagnosticVersion', { version: diagnostics.cloudflared_version })}
                    </span>
                  )}
                </div>
                <Button
                  type="button"
                  variant="secondary"
                  size="xs"
                  disabled={diagnosing || savingSettings}
                  onClick={runDiagnostics}
                >
                  <Activity className="size-3.5" />
                  {diagnosing ? t('remoteAccess.runningDiagnostics') : t('remoteAccess.runDiagnostics')}
                </Button>
              </div>
              {diagnosticRows.length > 0 && (
                <div className="mt-3 grid border-y border-border/70 sm:grid-cols-3">
                  {diagnosticRows.map((row, index) => (
                    <div
                      key={row.key}
                      className={`flex min-h-11 items-center justify-between gap-2 px-3 py-2 ${index > 0 ? 'border-t border-border/70 sm:border-l sm:border-t-0' : ''}`}
                    >
                      <span className="text-[11px] text-muted">{row.label}</span>
                      <Badge
                        title={diagnosticTitle(row.key, row.status)}
                        variant={row.status === 'available' ? 'success' : row.status === 'unavailable' ? 'destructive' : 'secondary'}
                      >
                        {diagnosticLabel(row.key, row.status)}
                      </Badge>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </details>
      )}

      {showPairingForm ? (
        <div className="space-y-1.5 px-5 py-4">
          <label htmlFor="remote-access-pairing-key" className="block text-[12px] font-medium text-foreground">
            {t('remoteAccess.pairingKey')}
          </label>
          <div className="flex flex-col gap-2 md:flex-row md:items-center">
            <CompactField
              id="remote-access-pairing-key"
              className="min-w-0 flex-1 font-mono"
              value={pairingKey}
              onChange={(event) => setPairingKey(event.target.value)}
              placeholder="vrp_xxxxxxxxxxxxxxxxx"
            />
            <div className="flex shrink-0 flex-wrap gap-2">
              <Button
                type="button"
                variant="default"
                size="xs"
                className="font-semibold"
                disabled={pairing || !pairingKey.trim()}
                onClick={pair}
              >
                <Link2 className="size-3.5" />
                {pairing ? t('remoteAccess.pairing') : t('remoteAccess.pair')}
              </Button>
              {paired && (
                <Button
                  type="button"
                  variant="secondary"
                  size="xs"
                  onClick={() => {
                    setReconfiguring(false);
                    setPairingKey('');
                  }}
                >
                  {t('common.cancel')}
                </Button>
              )}
            </div>
          </div>
          <span className="block text-[10px] text-muted">{t('remoteAccess.pairingKeyHelp')}</span>
        </div>
      ) : (
        <div className="flex flex-col gap-3 px-5 py-4 md:flex-row md:items-center md:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[13px] font-medium text-mint-ink">
              <CheckCircle2 className="size-3.5" />
              {t('remoteAccess.configuredBadge')}
            </div>
            {publicUrl && (
              <a
                href={publicUrl}
                target="_blank"
                rel="noreferrer"
                className="mt-1 inline-flex max-w-full items-center gap-1 truncate font-mono text-[11px] text-cyan-ink hover:underline"
                title={publicUrl}
              >
                <span className="truncate">{publicUrl}</span>
                <ExternalLink className="size-3 shrink-0" />
              </a>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="secondary"
              size="xs"
              disabled={!running || optimizing || (qualityFresh && quality?.state === 'recovering')}
              onClick={optimizeRoute}
              title={t('remoteAccess.optimizeRoute')}
            >
              <Route className="size-3.5" />
              {optimizing || (qualityFresh && quality?.state === 'recovering')
                ? t('remoteAccess.optimizingRoute')
                : t('remoteAccess.optimizeRoute')}
            </Button>
            <Button
              type="button"
              variant="secondary"
              size="xs"
              onClick={() => setReconfiguring(true)}
            >
              {t('remoteAccess.repair')}
            </Button>
            <Button
              type="button"
              variant="secondary"
              size="xs"
              disabled={!paired || running}
              onClick={start}
            >
              {t('common.start')}
            </Button>
            <Button
              type="button"
              variant="secondary"
              size="xs"
              disabled={!paired || !running}
              onClick={stop}
            >
              {t('common.stop')}
            </Button>
          </div>
        </div>
      )}

      {actionMessage && (
        <div className={`border-t border-border px-4 py-3 text-[12px] ${
          actionMessage.type === 'error' ? 'text-gold-ink' : 'text-mint-ink'
        }`}>
          {actionMessage.text}
        </div>
      )}
    </section>
  );
};
