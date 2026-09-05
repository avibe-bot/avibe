import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ArrowUpRight,
  Brain,
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
import type { DependencyItem, InstallResult, MemoryStatusResult } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { dependencyHasInstallAction, memoryRuntimeSidecarRunning } from './SettingsDependenciesPage.logic';
import { errorMessage } from '@/lib/errorMessage';

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
  'model-hub-engine': { icon: Network, tileCls: 'bg-mint-soft', iconCls: 'text-mint-ink' },
  'show-runtime': { icon: LayoutDashboard, tileCls: 'bg-cyan-soft', iconCls: 'text-cyan-ink' },
  'memory-package': { icon: Brain, tileCls: 'bg-gold-soft', iconCls: 'text-gold-ink' },
  'memory-runtime': { icon: Brain, tileCls: 'bg-violet-soft', iconCls: 'text-violet-ink' },
  tmux: { icon: SquareTerminal, tileCls: 'bg-surface-3', iconCls: 'text-foreground' },
  node: { icon: Hexagon, tileCls: 'bg-violet-soft', iconCls: 'text-violet-ink' },
};

export const SettingsDependenciesPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const [deps, setDeps] = useState<DependencyItem[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [memoryStatus, setMemoryStatus] = useState<MemoryStatusResult | null>(null);
  const [memoryStatusLoaded, setMemoryStatusLoaded] = useState(false);

  const refreshDependencies = useCallback(async () => {
    try {
      const res = await api.listDependencies();
      setDeps(res.deps ?? []);
    } catch {
      setDeps([]);
    }
  }, [api]);

  const refreshMemoryStatus = useCallback(async () => {
    try {
      setMemoryStatus(await api.getMemoryStatus());
    } catch {
      setMemoryStatus(null);
    } finally {
      setMemoryStatusLoaded(true);
    }
  }, [api]);

  const refreshAll = useCallback(async () => {
    await Promise.all([refreshDependencies(), refreshMemoryStatus()]);
  }, [refreshDependencies, refreshMemoryStatus]);

  useEffect(() => {
    void refreshAll();
  }, [refreshAll]);

  // A closed backend reason/message is often a snake_case token (e.g.
  // `memory_runtime_unpublished`) rather than human copy. Localize any
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

  const install = async (dep: DependencyItem) => {
    if (busy !== null) return;
    setBusy(dep.id);
    try {
      const res = await api.installDependency(dep.id);
      showToast(
        res.ok
          ? t('settings.dependencies.installed', { name: t(`settings.dependencies.items.${dep.id}.label`) })
          : localizedFailure(res),
        res.ok ? 'success' : 'error'
      );
      await refreshAll();
    } catch (e) {
      showToast(errorMessage(e) || t('settings.dependencies.installFailed'), 'error');
    } finally {
      setBusy(null);
    }
  };

  const statusText = (d: DependencyItem) => {
    // Closed non-installed failure states render distinctly, ahead
    // of the generic "not installed" fallback.
    if (d.status === 'unsupported') return t('settings.dependencies.statusUnsupported');
    if (d.status === 'error') return t('settings.dependencies.statusError');
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
    if (d.status === 'not_required') return 'secondary';
    if (d.status === 'error') return 'destructive';
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
    if (d.id === 'show-runtime' || d.id === 'memory-runtime') {
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
        <Button variant="secondary" size="sm" onClick={() => void refreshAll()}>
          <RefreshCw className="size-3.5" />
          {t('settings.dependencies.recheckAll')}
        </Button>
      }
    >
      {deps === null ? (
        <div className="text-sm text-muted">{t('common.loading')}</div>
      ) : (
        <div className="flex flex-col gap-3.5">
          <div className="flex items-center gap-3 rounded-xl border border-mint/30 bg-mint/[0.08] px-5 py-3.5">
            <ShieldCheck className="size-4 shrink-0 text-mint-ink" />
            <span className="text-[13px] leading-snug text-foreground">{t('settings.dependencies.autoBanner')}</span>
          </div>

          {deps.map((d) => {
            const meta = DEP_META[d.id] ?? DEP_META.node;
            const installing = busy === d.id;
            const showAction = dependencyHasInstallAction(d);
            const isMemoryRuntime = d.id === 'memory-runtime';
            const sidecarRunning = isMemoryRuntime && memoryRuntimeSidecarRunning(memoryStatus);
            const repairBlockedBySidecar = isMemoryRuntime && (!memoryStatusLoaded || sidecarRunning);
            const dependencyOperationBusy = busy !== null;
            const persistedFailure = d.status === 'error' && d.reason
              ? localizedReason(d.reason, t('settings.dependencies.installFailed'))
              : null;
            return (
              <SettingsResourceRow
                key={d.id}
                icon={meta.icon}
                tileClassName={meta.tileCls}
                iconClassName={meta.iconCls}
                title={t(`settings.dependencies.items.${d.id}.label`)}
                badges={
                  d.required && (
                    <Badge variant="secondary" className="font-mono uppercase tracking-[0.08em]">
                      {t('settings.dependencies.required')}
                    </Badge>
                  )
                }
                detail={
                  <>
                    {t(`settings.dependencies.items.${d.id}.detail`)}
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
                    <Badge variant={statusVariant(d)} className="font-mono">
                      {statusText(d)}
                    </Badge>
                    {isMemoryRuntime && d.installed && (
                      <Button asChild variant="secondary" size="xs">
                        <Link to="/settings/memory">
                          {t('common.configure')}
                          <ArrowUpRight className="size-3.5" />
                        </Link>
                      </Button>
                    )}
                    {showAction && (
                      <Button
                        variant={d.installed ? 'secondary' : 'brand'}
                        size="xs"
                        disabled={dependencyOperationBusy || repairBlockedBySidecar}
                        onClick={() => void install(d)}
                      >
                        {installing ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : d.installed || d.status === 'error' ? (
                          <RefreshCw className="size-3.5" />
                        ) : (
                          <Download className="size-3.5" />
                        )}
                        {actionText(d, installing)}
                      </Button>
                    )}
                  </>
                }
                footer={isMemoryRuntime && sidecarRunning ? (
                  <div className="border-t border-border pt-3 text-[11px] leading-snug text-muted">
                    {t('settings.dependencies.memoryRuntimeDisableBeforeRepair')}
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
      )}
    </SettingsPageShell>
  );
};
