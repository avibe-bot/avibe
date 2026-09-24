import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ArrowUpRight,
  Download,
  Hexagon,
  KeyRound,
  LayoutDashboard,
  Loader2,
  Network,
  RefreshCw,
  ShieldCheck,
  SquareTerminal,
  Terminal,
  WandSparkles,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import { SettingsPageShell } from './SettingsPageShell';
import { SettingsResourceRow } from './SettingsPrimitives';
import { useApi } from '@/context/ApiContext';
import type { DependencyItem, InstallResult } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import {
  dependencyIsStartupRepairing,
  dependencyHasInstallAction,
  dependencyNeedsStartupRepair,
} from './SettingsDependenciesPage.logic';
import { errorMessage } from '@/lib/errorMessage';
import { useDependencyChecks } from './useDependencyChecks';

// Mirrors design.pen "vibe-remote — Settings · Dependencies": one card per
// required local runtime (icon tile + name/REQUIRED + detail + status pill +
// action), reusing the Backends-page card shape. askill + the Show Page
// runtime and CPA auto-install during `vibe runtime prepare`; this page surfaces
// their status and offers manual re-check / install / repair. Backend CLIs are
// managed on the Backends tab — linked, not duplicated.

type DepMeta = { icon: LucideIcon; tileCls: string; iconCls: string };

const DEP_META: Record<string, DepMeta> = {
  askill: { icon: WandSparkles, tileCls: 'bg-mint-soft', iconCls: 'text-mint-ink' },
  avault: { icon: KeyRound, tileCls: 'bg-gold-soft', iconCls: 'text-gold-ink' },
  'show-runtime': { icon: LayoutDashboard, tileCls: 'bg-cyan-soft', iconCls: 'text-cyan-ink' },
  'model-hub-engine': { icon: Network, tileCls: 'bg-mint-soft', iconCls: 'text-mint-ink' },
  tmux: { icon: SquareTerminal, tileCls: 'bg-surface-3', iconCls: 'text-foreground' },
  node: { icon: Hexagon, tileCls: 'bg-violet-soft', iconCls: 'text-violet-ink' },
};

export const SettingsDependenciesPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const { checks, refresh: refreshDependencies, checking } = useDependencyChecks(api.listDependencies);
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => { void refreshDependencies(); }, [refreshDependencies]);
  // A closed backend reason/message is often a snake_case token rather than human copy. Localize any
  // token-shaped string through the shared errors namespace so the user never
  // sees a raw identifier; fall back to a human message or the generic failure.
  const localizedReason = (token: string | null | undefined, fallback: string): string => {
    if (typeof token === 'string' && /^[a-z][a-z0-9_]*$/.test(token)) {
      return t(`errors.${token}`, { defaultValue: t('settings.dependencies.installFailed') });
    }
    return fallback;
  };

  const localizedFailure = (res: InstallResult): string => {
    return localizedReason(
      res.reason || res.message,
      res.message || t('settings.dependencies.installFailed')
    );
  };

  const install = async (dep: DependencyItem, displayId: string) => {
    if (busy !== null) return;
    setBusy(dep.id);
    try {
      const res = await api.installDependency(dep.id);
      showToast(
        res.ok
          ? t('settings.dependencies.installed', { name: t(`settings.dependencies.items.${displayId}.label`, { defaultValue: `settings.dependencies.items.${displayId}.label` }) })
          : localizedFailure(res),
        res.ok ? 'success' : 'error'
      );
    } catch (e) {
      showToast(errorMessage(e) || t('settings.dependencies.installFailed'), 'error');
    } finally {
      await refreshDependencies(dep.id);
      setBusy(null);
    }
  };

  const statusText = (d: DependencyItem) => {
    if (checks[d.id]?.reconciling && dependencyIsStartupRepairing(d, checks[d.id].reconcilingDependencies)) return t('settings.dependencies.installing');
        // Closed non-installed failure states render distinctly, ahead
    // of the generic "not installed" fallback.
    if (d.status === 'unsupported') return t('settings.dependencies.statusUnsupported');
    if (d.status === 'error') return t('settings.dependencies.statusError');
    if (d.status === 'unknown') return t('settings.dependencies.statusUnknown');
    if (d.status === 'not_required') return t('settings.dependencies.statusNotRequired');
    if (!d.installed) return t('settings.dependencies.statusMissing');
    if (d.status === 'upgrade_required') {
      const word = t('settings.dependencies.statusUpgradeRequired');
      return d.version ? `${word} · v${String(d.version).replace(/^v/i, '')}` : word;
    }
    const word = d.kind === 'node' ? t('settings.dependencies.statusDetected') : t('settings.dependencies.statusReady');
    return d.version ? `${word} · v${String(d.version).replace(/^v/i, '')}` : word;
  };

  const statusVariant = (d: DependencyItem): 'secondary' | 'success' | 'warning' | 'destructive' => {
    if (checks[d.id]?.reconciling && dependencyIsStartupRepairing(d, checks[d.id].reconcilingDependencies)) return 'warning';
    if (d.status === 'not_required') return 'secondary';
    if (d.status === 'error') return 'destructive';
    if (d.status === 'unknown') return 'warning';
    if (d.status === 'unsupported' || d.status === 'upgrade_required') return 'warning';
    return d.installed ? 'success' : 'destructive';
  };

  const actionText = (d: DependencyItem, installing: boolean): string => {
    if (installing) return t('settings.dependencies.installing');
    if (d.status === 'upgrade_required') return t('settings.dependencies.update');
    if (d.id === 'model-hub-engine' && d.status === 'error') {
      return t('settings.dependencies.repair');
    }
    if (!d.installed) return t('settings.dependencies.install');
    if (d.id === 'show-runtime') {
      return t('settings.dependencies.repair');
    }
    return t('settings.dependencies.reinstall');
  };

  return (
    <SettingsPageShell
      activeTab="dependencies"
      title={t('settings.dependenciesTitle')}
      subtitle={t('settings.dependenciesSubtitle')}
      actions={
        <Button variant="secondary" size="sm" disabled={checking || busy !== null} onClick={() => void refreshDependencies()}>
          <RefreshCw className="size-3.5" />
          {t('settings.dependencies.recheckAll')}
        </Button>
      }
    >
        <div className="flex flex-col gap-3.5">
          <div className="flex items-center gap-3 rounded-xl border border-mint/30 bg-mint/[0.08] px-5 py-3.5">
            <ShieldCheck className="size-4 shrink-0 text-mint-ink" />
            <span className="text-[13px] leading-snug text-foreground">{t('settings.dependencies.autoBanner')}</span>
          </div>

          {Object.entries(DEP_META).map(([id, meta]) => {
            const check = checks[id];
            const d = check.data;
            const checkFailure = check.error
              ? t(`settings.dependencies.${check.error === 'timeout' ? 'checkTimeout' : 'checkFailed'}`)
              : null;
            const retryCheck = check.error && !check.checking ? (
              <Button
                variant="secondary"
                size="icon"
                className="size-8"
                title={t('settings.dependencies.recheck')}
                aria-label={t('settings.dependencies.recheck')}
                disabled={busy !== null}
                onClick={() => void refreshDependencies(id)}
              >
                <RefreshCw className="size-3.5" />
              </Button>
            ) : null;
            const checkProgress = check.checking ? (
              <Loader2
                className="size-3.5 shrink-0 animate-spin text-muted"
                aria-label={t('settings.dependencies.checking')}
              />
            ) : null;
            if (!d) {
              return (
                <SettingsResourceRow
                  key={id}
                  icon={meta.icon}
                  tileClassName={meta.tileCls}
                  iconClassName={meta.iconCls}
                  title={t(`settings.dependencies.items.${id}.label`, { defaultValue: `settings.dependencies.items.${id}.label` })}
                  detail={t(`settings.dependencies.items.${id}.detail`, { defaultValue: `settings.dependencies.items.${id}.detail` })}
                  actions={
                    <>
                      {checkProgress}
                      <Badge variant={check.error ? 'destructive' : 'secondary'} className="font-mono">
                        {checkFailure || t('settings.dependencies.checking')}
                      </Badge>
                      {retryCheck}
                    </>
                  }
                />
              );
            }
            const installing = busy === d.id;
            const startupInstalling = Boolean(check.reconciling) && dependencyIsStartupRepairing(d, check.reconcilingDependencies);
            const startupRepairPending = Boolean(check.reconciling) && dependencyNeedsStartupRepair(d);
            const showAction = dependencyHasInstallAction(d);
            const repairBlockedBySidecar = false;
            const dependencyOperationBusy = busy !== null || check.checking || check.error !== null;
            const notice = null;
            const persistedFailure = d.status === 'error' && d.reason
              ? localizedReason(d.reason, t('settings.dependencies.installFailed'))
              : null;
            return (
              <SettingsResourceRow
                key={id}
                icon={meta.icon}
                tileClassName={meta.tileCls}
                iconClassName={meta.iconCls}
                title={t(`settings.dependencies.items.${id}.label`, { defaultValue: `settings.dependencies.items.${id}.label` })}
                badges={
                  d.required && (
                    <Badge variant="secondary" className="font-mono uppercase tracking-[0.08em]">
                      {t('settings.dependencies.required')}
                    </Badge>
                  )
                }
                detail={
                  <>
                    {t(`settings.dependencies.items.${id}.detail`, { defaultValue: `settings.dependencies.items.${id}.detail` })}
                    {d.id === 'model-hub-engine' && d.latest_version && (
                      <span className="mt-1 block font-mono text-[11px]">
                        {t('settings.dependencies.targetVersion', {
                          version: String(d.latest_version).replace(/^v/i, ''),
                        })}
                      </span>
                    )}
                  </>
                }
                actions={
                  <>
                    {checkProgress}
                    <Badge variant={check.error ? 'destructive' : statusVariant(d)} className="font-mono">
                      {checkFailure || statusText(d)}
                    </Badge>
                    {retryCheck}
                    {showAction && (
                      <Button
                        variant={d.installed ? 'secondary' : 'brand'}
                        size="xs"
                        disabled={dependencyOperationBusy || repairBlockedBySidecar || startupInstalling || startupRepairPending}
                        onClick={() => void install(d, id)}
                      >
                        {installing || startupInstalling ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : d.installed || d.status === 'error' ? (
                          <RefreshCw className="size-3.5" />
                        ) : (
                          <Download className="size-3.5" />
                        )}
                        {actionText(d, installing || startupInstalling)}
                      </Button>
                    )}
                  </>
                }
                footer={checkFailure ? (
                  <div role="alert" className="border-t border-destructive/30 pt-3 text-[11px] leading-snug text-destructive-ink">
                    {checkFailure}
                    {persistedFailure && <div className="mt-1">{persistedFailure}</div>}
                    {notice && <div className="mt-1 text-muted">{notice}</div>}
                  </div>
                ) : notice ? (
                  <div className="border-t border-border pt-3 text-[11px] leading-snug text-muted">
                    {notice}
                    {persistedFailure && <div role="alert" className="mt-1 text-destructive-ink">{persistedFailure}</div>}
                  </div>
                ) : persistedFailure ? (
                  <div
                    role="alert"
                    className="border-t border-destructive/30 pt-3 text-[11px] leading-snug text-destructive-ink"
                  >
                    {persistedFailure}
                  </div>
                ) : undefined}
              />
            );
          })}

          <SettingsResourceRow
            icon={Terminal}
            tileClassName="bg-surface-3"
            iconClassName="text-muted"
            className="opacity-70"
            title={t('settings.dependencies.backendsTitle')}
            detail={t('settings.dependencies.backendsDetail')}
            actions={
              <Button asChild variant="secondary" size="xs">
                <Link to="/settings/backends">
                  {t('settings.dependencies.manageBackends')}
                  <ArrowUpRight className="size-3.5" />
                </Link>
              </Button>
            }
          />
        </div>
    </SettingsPageShell>
  );
};
