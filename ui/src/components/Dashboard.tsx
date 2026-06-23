import React, { useEffect, useMemo, useState } from 'react';
import {
  Activity,
  ArrowRight,
  Cloud,
  Clock,
  Cpu,
  Hash,
  MessageSquare,
  Play,
  PlugZap,
  RotateCw,
  Square,
  Zap,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { useApi, type LogEntry } from '@/context/ApiContext';
import { useStatus } from '@/context/StatusContext';
import { PlatformIcon } from '@/components/visual';
import { Button } from '@/components/ui/button';
import {
  getEnabledPlatforms,
  getPlatformCatalog,
  isWorkbenchPlatform,
  platformSupportsChannels,
} from '@/lib/platforms';

function relativeFromIso(value?: string | null) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const diffMs = date.getTime() - Date.now();
  const diffSec = Math.round(diffMs / 1000);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  if (Math.abs(diffSec) < 60) return rtf.format(diffSec, 'second');
  const diffMin = Math.round(diffSec / 60);
  if (Math.abs(diffMin) < 60) return rtf.format(diffMin, 'minute');
  const diffHour = Math.round(diffMin / 60);
  return rtf.format(diffHour, 'hour');
}

function absoluteFromIso(value?: string | null) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function summarizeLog(message: string) {
  return message.replace(/^\[[^\]]+\]\s*-?\s*/, '');
}

// Mirrors design.pen NbtYJ (Card/Stat): cornerRadius 12, fill --background,
// stroke --border 1px, padding 20, gap 6. Top row (justify space-between)
// label 13px + icon 16px muted; value 28px bold -0.4 tracking; trend 12px muted.
const statCardClassName =
  'group flex min-h-[126px] flex-col gap-1.5 rounded-xl border border-border bg-background p-5 transition hover:border-border-strong hover:bg-surface-2/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/45';

const StatCard: React.FC<{
  label: string;
  value: string;
  hint: string;
  icon: React.ReactNode;
  to: string;
}> = ({ label, value, hint, icon, to }) => (
  <Link to={to} className={statCardClassName}>
    <div className="flex items-center justify-between gap-2">
      <span className="text-[13px] font-medium text-muted">{label}</span>
      <span className="text-muted transition group-hover:text-foreground">{icon}</span>
    </div>
    <div className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">{value}</div>
    <div className="text-[12px] font-medium text-muted">{hint}</div>
  </Link>
);

const CloudStatCard: React.FC<{
  label: string;
  value: string;
  hint: string;
  icon: React.ReactNode;
  cloudHomeUrl: string;
  settingsHref: string;
  publicUrl?: string | null;
}> = ({ label, value, hint, icon, cloudHomeUrl, settingsHref, publicUrl }) => (
  <div className={clsx(statCardClassName, 'relative')}>
    <Link
      to={settingsHref}
      className="absolute inset-0 z-0 rounded-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/45"
      aria-label={hint}
    />
    <div className="pointer-events-none relative z-10 flex items-center justify-between gap-2">
      <a
        href={cloudHomeUrl}
        target="_blank"
        rel="noreferrer"
        className="pointer-events-auto text-[13px] font-medium text-muted transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/45"
      >
        {label}
      </a>
      <span className="text-muted transition group-hover:text-foreground">{icon}</span>
    </div>
    {publicUrl ? (
      <a
        href={publicUrl}
        target="_blank"
        rel="noreferrer"
        className="pointer-events-auto relative z-10 w-fit text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground transition hover:text-mint focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-mint/45"
      >
        {value}
      </a>
    ) : (
      <div className="pointer-events-none relative z-10 text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">{value}</div>
    )}
    <div className="pointer-events-none relative z-10 text-[12px] font-medium text-muted">{hint}</div>
  </div>
);

export const Dashboard: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { status, control } = useStatus();
  const [config, setConfig] = useState<any>(null);
  const [doctor, setDoctor] = useState<any>(null);
  const [remoteAccess, setRemoteAccess] = useState<any>(null);
  const [settingsByPlatform, setSettingsByPlatform] = useState<Record<string, any>>({});
  const [usersCount, setUsersCount] = useState(0);
  const [recentLogs, setRecentLogs] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const nextConfig = await api.getConfig();
        if (cancelled) return;
        setConfig(nextConfig);

        const enabledPlatforms = getEnabledPlatforms(nextConfig);
        const [doctorRes, remoteAccessRes, logsRes, settingsEntries, usersEntries] = await Promise.all([
          api.doctor(),
          api.remoteAccessStatus().catch(() => null),
          api.getLogs(8, 'service'),
          Promise.all(enabledPlatforms.map(async (platform) => [platform, await api.getSettings(platform)] as const)),
          Promise.all(enabledPlatforms.map(async (platform) => [platform, await api.getUsers(platform)] as const)),
        ]);

        if (cancelled) return;
        setDoctor(doctorRes);
        setRemoteAccess(remoteAccessRes);
        setRecentLogs((logsRes.logs || []).slice(-5).reverse());
        setSettingsByPlatform(Object.fromEntries(settingsEntries));
        setUsersCount(
          usersEntries.reduce((sum, [, payload]) => sum + Object.keys(payload?.users || {}).length, 0)
        );
      } catch {
        if (!cancelled) {
          setRecentLogs([]);
        }
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [api]);

  const handleAction = async (action: string) => {
    setLoading(true);
    try {
      await control(action);
    } catch (e) {
      console.error('Service control action failed', e);
    } finally {
      setLoading(false);
    }
  };

  const platformCatalog = useMemo(() => getPlatformCatalog(config), [config]);
  const enabledPlatforms = useMemo(() => getEnabledPlatforms(config), [config]);
  const lastUpdated = relativeFromIso(status.updated_at);
  const startedAtRelative = relativeFromIso(status.started_at);
  const startedAtAbsolute = absoluteFromIso(status.started_at);
  const totalActiveGroups = useMemo(
    () =>
      Object.values(settingsByPlatform).reduce(
        (sum, payload: any) =>
          sum + Object.values(payload?.channels || {}).filter((item: any) => item?.enabled).length,
        0
      ),
    [settingsByPlatform]
  );
  const totalDiscoveredGroups = useMemo(
    () =>
      Object.values(settingsByPlatform).reduce(
        (sum, payload: any) => sum + Object.keys(payload?.channels || {}).length,
        0
      ),
    [settingsByPlatform]
  );

  // Surface the always-on Avibe Workbench first; IM transports keep catalog
  // order behind it (Array.prototype.sort is stable).
  const orderedPlatforms = [...platformCatalog].sort(
    (a, b) => Number(isWorkbenchPlatform(b.id)) - Number(isWorkbenchPlatform(a.id))
  );

  const platformCards = orderedPlatforms.map((platform) => {
    const platformSettings = settingsByPlatform[platform.id] || {};
    const groups = platformSettings.channels || {};
    const activeGroups = Object.values(groups).filter((item: any) => item?.enabled).length;
    const discoveredGroups = Object.keys(groups).length;
    const isWorkbench = isWorkbenchPlatform(platform.id);
    const enabled = enabledPlatforms.includes(platform.id);
    const supportsGroups = platformSupportsChannels(config, platform.id);

    // The workbench has no externally-discovered groups, so the group-count
    // summary is meaningless there — leave the subtitle empty.
    const hint = isWorkbench
      ? null
      : supportsGroups
        ? t('dashboard.platformGroupsHint')
            .replace('{{active}}', String(activeGroups))
            .replace('{{discovered}}', String(discoveredGroups))
        : t('dashboard.platformNoGroupsHint');

    return {
      id: platform.id,
      title: t(platform.title_key || `platform.${platform.id}.title`),
      // ``enabled`` still drives the "Active platforms" metric titles below,
      // which count only configured IM transports. The status pill uses
      // ``connected`` instead: the in-process workbench is always live, so it
      // can never read as "not configured".
      enabled,
      connected: isWorkbench || enabled,
      hint,
      // The workbench's action opens the local workbench home rather than IM
      // group routing.
      actionHref: isWorkbench ? '/' : supportsGroups ? '/admin/groups' : '/settings/platforms',
      actionLabel: isWorkbench || supportsGroups ? t('dashboard.manageRoute') : t('dashboard.configure'),
    };
  });

  const isRunning = status.state === 'running';
  const rawCloudPublicUrl = String(remoteAccess?.public_url || '');
  const cloudPublicUrl = rawCloudPublicUrl.replace(/^https?:\/\//, '');
  const cloudPublicHref = remoteAccess?.paired && rawCloudPublicUrl
    ? rawCloudPublicUrl.match(/^https?:\/\//)
      ? rawCloudPublicUrl
      : `https://${rawCloudPublicUrl}`
    : null;
  const cloudValue = remoteAccess?.paired
    ? remoteAccess?.running
      ? t('dashboard.metricCloudConnected')
      : t('dashboard.metricCloudConfigured')
    : t('dashboard.metricCloudDisconnected');
  const cloudHint = remoteAccess?.paired
    ? remoteAccess?.running
      ? t('dashboard.metricCloudHintConnected', { url: cloudPublicUrl || 'avibe.bot' })
      : t('dashboard.metricCloudHintConfigured')
    : t('dashboard.metricCloudHintDisconnected');

  // The always-on Avibe Workbench counts as a connected platform (it can never
  // be "not configured"), so the metric value + its titles use ``connected`` —
  // consistent with the platform list below and the catalog denominator, which
  // both already include the workbench.
  const connectedPlatformCards = platformCards.filter((p) => p.connected);
  const activePlatformCount = connectedPlatformCards.length;
  const enabledPlatformTitles = connectedPlatformCards.map((p) => p.title).join(' · ');

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-1.5">
        <h1 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">
          {t('dashboard.title')}
        </h1>
        <p className="text-[14px] text-muted">{t('dashboard.subtitle')}</p>
      </div>

      {/* Mirrors design.pen kgGuL (heroStatus): cornerRadius 16, fill --surface-2,
          mint stroke 33%, padding [28, 32], shadow blur 48 #5BFFA014 y24 spread -12 */}
      <div
        className={clsx(
          'flex flex-wrap items-center justify-between gap-6 rounded-2xl border bg-surface-2 px-6 py-7 md:px-8',
          'shadow-[0_24px_48px_-12px_rgba(91,255,160,0.078)]',
          isRunning ? 'border-mint/30' : 'border-border'
        )}
      >
        <div className="flex min-w-0 items-center gap-5">
          {/* heroPulse: 52×52 mint-soft circle, blur 24 spread -6 #5BFFA070 glow */}
          <div
            className={clsx(
              'flex size-[52px] shrink-0 items-center justify-center rounded-full border',
              isRunning
                ? 'border-mint/35 bg-mint/[0.08] shadow-[0_0_24px_-6px_rgba(91,255,160,0.44)]'
                : 'border-border bg-foreground/[0.04]'
            )}
          >
            <Zap
              className={clsx('size-5', isRunning ? 'text-mint' : 'text-muted')}
              strokeWidth={2.25}
            />
          </div>
          <div className="flex min-w-0 flex-col gap-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <span
                className={clsx(
                  'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-[0.14em]',
                  isRunning
                    ? 'border-mint/30 bg-mint/[0.08] text-mint'
                    : 'border-border bg-foreground/[0.04] text-muted'
                )}
              >
                <span
                  className={clsx(
                    'size-1.5 rounded-full',
                    isRunning ? 'bg-mint shadow-[0_0_8px_rgba(91,255,160,0.9)]' : 'bg-muted'
                  )}
                />
                {isRunning ? t('dashboard.runningTitle') : t('dashboard.stoppedTitle')}
              </span>
              {!doctor?.ok && (
                <span className="inline-flex items-center rounded-full border border-gold/30 bg-gold/10 px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-gold">
                  {t('dashboard.levelAttention')}
                </span>
              )}
            </div>
            <div className="text-[18px] font-semibold leading-snug text-foreground">
              {t('dashboard.heroTitle')}
            </div>
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 font-mono text-[11px] leading-none text-muted">
              <span className="inline-flex items-center gap-1">
                <Cpu className="size-3 shrink-0" strokeWidth={2} />
                {status.service_pid || status.pid
                  ? `PID ${status.service_pid || status.pid}`
                  : t('common.unknown')}
              </span>
              {isRunning && startedAtAbsolute && (
                <span
                  className="inline-flex items-center gap-1"
                  title={status.started_at as string | undefined}
                >
                  <Clock className="size-3 shrink-0" strokeWidth={2} />
                  {t('dashboard.startedAt', {
                    time: startedAtAbsolute,
                    relative: startedAtRelative ?? '',
                  })}
                </span>
              )}
              {!isRunning && lastUpdated && (
                <span className="inline-flex items-center gap-1">
                  <Clock className="size-3 shrink-0" strokeWidth={2} />
                  {lastUpdated}
                </span>
              )}
              {status.last_action && <span>· {status.last_action}</span>}
            </div>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {!isRunning && (
            <Button
              type="button"
              variant="brand"
              size="sm"
              onClick={() => void handleAction('start')}
              disabled={loading}
            >
              <Play className="size-3.5" strokeWidth={2.5} />
              {t('common.start')}
            </Button>
          )}
          {isRunning && (
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => void handleAction('stop')}
              disabled={loading}
            >
              <Square className="size-3.5" strokeWidth={2.5} />
              {t('common.stop')}
            </Button>
          )}
          <Button
            type="button"
            variant={isRunning ? 'brand' : 'secondary'}
            size="sm"
            onClick={() => void handleAction('restart')}
            disabled={loading}
          >
            <RotateCw className="size-3.5" strokeWidth={2.5} />
            {t('common.restart')}
          </Button>
        </div>
      </div>

      {/* Mirrors design.pen statsRow + NbtYJ Card/Stat */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {/* Avibe (remote access) card leads the row. */}
        <CloudStatCard
          label={t('dashboard.metricCloud')}
          value={cloudValue}
          hint={cloudHint}
          icon={<Cloud className="size-4" />}
          cloudHomeUrl="https://avibe.bot"
          settingsHref="/admin/remote-access"
          publicUrl={cloudPublicHref}
        />
        <StatCard
          label={t('dashboard.metricPlatforms')}
          value={`${activePlatformCount} / ${platformCatalog.length}`}
          hint={enabledPlatformTitles || t('dashboard.metricPlatformsHint')}
          icon={<PlugZap className="size-4" />}
          to="/admin/settings/platforms"
        />
        <StatCard
          label={t('dashboard.metricGroups')}
          value={String(totalActiveGroups)}
          hint={t('dashboard.metricGroupsHint').replace('{{count}}', String(totalDiscoveredGroups))}
          icon={<Hash className="size-4" />}
          to="/admin/groups"
        />
        <StatCard
          label={t('dashboard.metricUsers')}
          value={String(usersCount)}
          hint={t('dashboard.metricUsersHint')}
          icon={<MessageSquare className="size-4" />}
          to="/admin/users"
        />
      </div>

      {/* Mirrors design.pen l0yMzY (twoCol) — platformsCard 1.2fr / activityCard 420px.
          Both: cornerRadius 12, fill --background, stroke --border 1px,
          head padding [20, 24] with bottom border, body fill_container. */}
      <div className="grid gap-6 xl:grid-cols-[1.2fr_0.8fr]">
        <div className="flex flex-col overflow-hidden rounded-xl border border-border bg-background">
          <div className="flex items-center justify-between gap-4 border-b border-border px-6 py-5">
            <div className="flex flex-col gap-0.5">
              <h2 className="text-[15px] font-semibold text-foreground">
                {t('dashboard.platformOverviewTitle')}
              </h2>
              <p className="text-[12px] text-muted">{t('dashboard.platformOverviewSubtitle')}</p>
            </div>
            <Link
              to="/admin/settings/platforms"
              className="inline-flex items-center gap-1 rounded-lg border border-border bg-foreground/[0.04] px-3 py-1.5 text-[12px] font-medium text-foreground transition hover:border-border-strong"
            >
              {t('dashboard.manageAll')}
              <ArrowRight className="size-3.5" strokeWidth={2.25} />
            </Link>
          </div>
          <div className="flex flex-col divide-y divide-border">
            {platformCards.map((platform) => (
              <div key={platform.id} className="flex items-center justify-between gap-4 px-6 py-4">
                <div className="flex min-w-0 items-center gap-3">
                  <div className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-2">
                    <PlatformIcon platform={platform.id as any} size={18} />
                  </div>
                  <div className="flex min-w-0 flex-col gap-0.5">
                    <div className="text-[13px] font-semibold text-foreground">{platform.title}</div>
                    {platform.hint && (
                      <div className="text-[11px] text-muted">{platform.hint}</div>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <span
                    className={clsx(
                      'inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-medium',
                      platform.connected
                        ? 'border-mint/30 bg-mint/[0.08] text-mint'
                        : 'border-border bg-foreground/[0.04] text-muted'
                    )}
                  >
                    {platform.connected ? t('dashboard.connected') : t('dashboard.notConfigured')}
                  </span>
                  <Link
                    to={platform.actionHref}
                    className="inline-flex items-center gap-1 text-[12px] font-medium text-foreground transition hover:text-mint"
                  >
                    {platform.actionLabel}
                    <ArrowRight className="size-3.5" strokeWidth={2.25} />
                  </Link>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="flex flex-col overflow-hidden rounded-xl border border-border bg-background">
          <div className="flex items-center justify-between gap-4 border-b border-border px-6 py-5">
            <div className="flex flex-col gap-0.5">
              <h2 className="text-[15px] font-semibold text-foreground">
                {t('dashboard.recentActivityTitle')}
              </h2>
              <p className="text-[12px] text-muted">{t('dashboard.recentActivitySubtitle')}</p>
            </div>
            <Link
              to="/admin/logs"
              className="inline-flex items-center gap-1 text-[12px] font-medium text-foreground transition hover:text-mint"
            >
              {t('common.viewLogs')}
              <ArrowRight className="size-3.5" strokeWidth={2.25} />
            </Link>
          </div>
          <div className="flex flex-col divide-y divide-border">
            {recentLogs.length === 0 ? (
              <div className="px-6 py-8 text-center text-[12px] text-muted">
                {t('dashboard.recentActivityEmpty')}
              </div>
            ) : (
              recentLogs.map((entry, index) => (
                <div
                  key={`${entry.timestamp}-${index}`}
                  className="flex items-start gap-3 px-6 py-3.5"
                >
                  <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border border-cyan/30 bg-cyan/[0.08] text-cyan">
                    <Activity className="size-3.5" strokeWidth={2.25} />
                  </div>
                  <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <div className="line-clamp-2 text-[12px] font-medium text-foreground">
                      {summarizeLog(entry.message)}
                    </div>
                    <div className="font-mono text-[10px] text-muted">
                      {relativeFromIso(entry.timestamp) || entry.timestamp} · {entry.logger}
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
