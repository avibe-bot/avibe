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
  RefreshCw,
  ShieldCheck,
  SquareTerminal,
  Terminal,
  Trash2,
  WandSparkles,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { SettingsPageShell } from './SettingsPageShell';
import { SettingsResourceRow } from './SettingsPrimitives';
import { useApi } from '@/context/ApiContext';
import type { DependencyItem, InstallResult, MemorySettings } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { dependencyHasInstallAction } from './SettingsDependenciesPage.logic';
import { errorMessage } from '@/lib/errorMessage';
import type { MemoryFactoryResetResult } from '@/lib/memoryFactoryReset';
import { memoryErrorMessage } from '@/lib/memoryRead';

// Mirrors design.pen "vibe-remote — Settings · Dependencies": one card per
// required local runtime (icon tile + name/REQUIRED + detail + status pill +
// action), reusing the Backends-page card shape. askill + the Show Page
// runtime auto-install during `vibe runtime prepare`; this page surfaces their
// status and offers manual re-check / install / repair. Backend CLIs are
// managed on the Backends tab — linked, not duplicated.

type DepMeta = { icon: LucideIcon; tileCls: string; iconCls: string };

const DEP_META: Record<string, DepMeta> = {
  askill: { icon: WandSparkles, tileCls: 'bg-mint-soft', iconCls: 'text-mint' },
  avault: { icon: KeyRound, tileCls: 'bg-gold-soft', iconCls: 'text-gold' },
  'show-runtime': { icon: LayoutDashboard, tileCls: 'bg-cyan-soft', iconCls: 'text-cyan' },
  'memory-runtime': { icon: Brain, tileCls: 'bg-violet-soft', iconCls: 'text-violet' },
  tmux: { icon: SquareTerminal, tileCls: 'bg-surface-3', iconCls: 'text-foreground' },
  node: { icon: Hexagon, tileCls: 'bg-violet-soft', iconCls: 'text-violet' },
};

export const SettingsDependenciesPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const [deps, setDeps] = useState<DependencyItem[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [memorySettings, setMemorySettings] = useState<MemorySettings | null>(null);
  const [reinitializeOpen, setReinitializeOpen] = useState(false);
  const [reinitializeBusy, setReinitializeBusy] = useState(false);
  const [reinitializeResult, setReinitializeResult] = useState<MemoryFactoryResetResult | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await api.listDependencies();
      setDeps(res.deps ?? []);
    } catch {
      setDeps([]);
    }
  }, [api]);

  const refreshMemorySettings = useCallback(async () => {
    try {
      const res = await api.getMemorySettings();
      setMemorySettings(res.status === 'ok' ? res : null);
    } catch {
      setMemorySettings(null);
    }
  }, [api]);

  useEffect(() => {
    void refresh();
    void refreshMemorySettings();
  }, [refresh, refreshMemorySettings]);

  // A closed backend reason/message is often a snake_case token (e.g.
  // `memory_runtime_unpublished`) rather than human copy. Localize any
  // token-shaped string through the shared errors namespace so the user never
  // sees a raw identifier; fall back to a human message or the generic failure.
  const localizedFailure = (res: InstallResult): string => {
    const token = res.reason || res.message;
    if (typeof token === 'string' && /^[a-z][a-z0-9_]*$/.test(token)) {
      return t(`errors.${token}`, { defaultValue: t('settings.dependencies.installFailed') });
    }
    return res.message || t('settings.dependencies.installFailed');
  };

  const install = async (dep: DependencyItem) => {
    if (busy !== null || reinitializeBusy) return;
    setBusy(dep.id);
    try {
      const res = await api.installDependency(dep.id);
      showToast(
        res.ok
          ? t('settings.dependencies.installed', { name: t(`settings.dependencies.items.${dep.id}.label`) })
          : localizedFailure(res),
        res.ok ? 'success' : 'error'
      );
      await refresh();
    } catch (e) {
      showToast(errorMessage(e) || t('settings.dependencies.installFailed'), 'error');
    } finally {
      setBusy(null);
    }
  };

  const reinitializeMemory = async () => {
    if (reinitializeBusy || busy !== null) return;
    setReinitializeBusy(true);
    setReinitializeResult(null);
    try {
      const res = await api.factoryResetMemory();
      setReinitializeResult(res);
      setReinitializeOpen(false);
      showToast(
        res.ok
          ? t('memory.factoryReset.completed')
          : memoryErrorMessage(t, res.error || 'memory_factory_reset_failed'),
        res.ok ? 'success' : 'error',
      );
    } catch {
      setReinitializeOpen(false);
      showToast(t('memory.factoryReset.failed'), 'error');
    } finally {
      await Promise.all([refresh(), refreshMemorySettings()]);
      window.dispatchEvent(new Event('avibe:memory-settings-changed'));
      setReinitializeBusy(false);
    }
  };

  const statusText = (d: DependencyItem) => {
    // Closed non-installed failure states render distinctly, ahead
    // of the generic "not installed" fallback.
    if (d.status === 'unsupported') return t('settings.dependencies.statusUnsupported');
    if (d.status === 'error') return t('settings.dependencies.statusError');
    if (!d.installed) return t('settings.dependencies.statusMissing');
    if (d.status === 'upgrade_required') return t('settings.dependencies.statusUpgradeRequired');
    const word = d.kind === 'node' ? t('settings.dependencies.statusDetected') : t('settings.dependencies.statusReady');
    return d.version ? `${word} · v${String(d.version).replace(/^v/i, '')}` : word;
  };

  const statusVariant = (d: DependencyItem): 'success' | 'warning' | 'destructive' => {
    if (d.status === 'error') return 'destructive';
    if (d.status === 'unsupported' || d.status === 'upgrade_required') return 'warning';
    return d.installed ? 'success' : 'destructive';
  };

  return (
    <SettingsPageShell
      activeTab="dependencies"
      title={t('settings.dependenciesTitle')}
      subtitle={t('settings.dependenciesSubtitle')}
      actions={
        <Button variant="secondary" size="sm" onClick={() => void refresh()}>
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
            <ShieldCheck className="size-4 shrink-0 text-mint" />
            <span className="text-[13px] leading-snug text-foreground">{t('settings.dependencies.autoBanner')}</span>
          </div>

          {deps.map((d) => {
            const meta = DEP_META[d.id] ?? DEP_META.node;
            const installing = busy === d.id;
            const showAction = dependencyHasInstallAction(d);
            const isMemoryRuntime = d.id === 'memory-runtime';
            const memoryRuntimeReady = isMemoryRuntime && d.installed && d.status === 'ready';
            const dependencyOperationBusy = busy !== null;
            const reinitializeDisabled = !memoryRuntimeReady
              || memorySettings === null
              || dependencyOperationBusy
              || reinitializeBusy;
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
                detail={t(`settings.dependencies.items.${d.id}.detail`)}
                actions={
                  <>
                    <Badge variant={statusVariant(d)} className="font-mono">
                      {statusText(d)}
                    </Badge>
                    {isMemoryRuntime && d.installed && (
                      <Button asChild variant="secondary" size="xs">
                        <Link to="/admin/settings/memory">
                          {t('common.configure')}
                          <ArrowUpRight className="size-3.5" />
                        </Link>
                      </Button>
                    )}
                    {showAction && (
                      <Button
                        variant={d.installed ? 'secondary' : 'brand'}
                        size="xs"
                        disabled={dependencyOperationBusy || reinitializeBusy}
                        onClick={() => void install(d)}
                      >
                        {installing ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : d.installed ? (
                          <RefreshCw className="size-3.5" />
                        ) : (
                          <Download className="size-3.5" />
                        )}
                        {installing
                          ? t('settings.dependencies.installing')
                          : d.installed
                            ? d.id === 'show-runtime' || d.id === 'memory-runtime'
                              ? t('settings.dependencies.repair')
                              : t('settings.dependencies.reinstall')
                            : t('settings.dependencies.install')}
                      </Button>
                    )}
                  </>
                }
                footer={isMemoryRuntime ? (
                  <div className="flex flex-col gap-3 border-t border-destructive/25 pt-3">
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                      <div className="min-w-0">
                        <div className="text-[12px] font-semibold text-foreground">
                          {t('memory.factoryReset.button')}
                        </div>
                        <div className="mt-0.5 text-[11px] leading-snug text-muted">
                          {memoryRuntimeReady
                            ? memorySettings === null
                              ? t('memory.factoryReset.settingsUnavailable')
                              : t('memory.factoryReset.dependenciesHint')
                            : t('memory.factoryReset.artifactRepairRequired')}
                        </div>
                      </div>
                      <Button
                        variant="destructive"
                        size="xs"
                        disabled={reinitializeDisabled}
                        onClick={() => {
                          setReinitializeResult(null);
                          setReinitializeOpen(true);
                        }}
                      >
                        {reinitializeBusy ? <Loader2 className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />}
                        {memorySettings?.factory_reset_required
                          ? t('memory.factoryReset.retry')
                          : t('memory.factoryReset.button')}
                      </Button>
                    </div>
                    {reinitializeResult ? (
                      <div role="status" className="text-[12px] text-foreground">
                        <div className="font-semibold">{t('memory.factoryReset.resultTitle')}</div>
                        <div className="mt-1 text-muted">
                          {reinitializeResult.ok
                            ? t('memory.factoryReset.resultCompleted')
                            : memoryErrorMessage(t, reinitializeResult.error || 'memory_factory_reset_failed')}
                        </div>
                        {reinitializeResult.roots ? (
                          <ul className="mt-2 flex flex-col gap-1 text-muted">
                            {reinitializeResult.roots.map((root) => (
                              <li key={root.path}>{t('memory.factoryReset.rootOutcome', {
                                path: root.path,
                                deleted: root.deleted
                                  ? root.error
                                    ? t('memory.factoryReset.partial')
                                    : t('memory.factoryReset.deleted')
                                  : root.existed === false
                                    ? t('memory.factoryReset.absent')
                                    : t('memory.factoryReset.retained'),
                              })}</li>
                            ))}
                          </ul>
                        ) : null}
                      </div>
                    ) : null}
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
                <Link to="/admin/settings/backends">
                  {t('settings.dependencies.manageBackends')}
                  <ArrowUpRight className="size-3.5" />
                </Link>
              </Button>
            }
          />
        </div>
      )}
      <ConfirmDialog
        open={reinitializeOpen}
        onOpenChange={setReinitializeOpen}
        destructive
        holdSeconds={5}
        title={t('memory.factoryReset.confirmTitle')}
        description={t('memory.factoryReset.confirmDescription')}
        confirmLabel={t('memory.factoryReset.confirmLabel')}
        confirmDisabled={memorySettings === null || reinitializeBusy || busy !== null || !deps?.some(
          (dep) => dep.id === 'memory-runtime' && dep.installed && dep.status === 'ready',
        )}
        onConfirm={reinitializeMemory}
      >
        <div className="flex flex-col gap-3 text-[12.5px] leading-snug">
          <div className="rounded-[10px] border border-border bg-surface-2 px-3 py-2.5">
            <div className="mb-1 font-semibold text-foreground">{t('memory.factoryReset.deletesTitle')}</div>
            <ul className="flex flex-col gap-1">
              {(t('memory.factoryReset.deletes', { returnObjects: true }) as string[]).map((line, idx) => (
                <li key={idx} className="flex gap-2 text-muted">
                  <span className="mt-1 size-1 shrink-0 rounded-full bg-muted" />
                  {line}
                </li>
              ))}
            </ul>
          </div>
          <div className="rounded-[10px] border border-warning/30 bg-warning/5 px-3 py-2.5">
            <div className="mb-1 font-semibold text-foreground">{t('memory.factoryReset.retainsTitle')}</div>
            <ul className="flex flex-col gap-1">
              {(t('memory.factoryReset.retains', { returnObjects: true }) as string[]).map((line, idx) => (
                <li key={idx} className="flex gap-2 text-muted">
                  <span className="mt-1 size-1 shrink-0 rounded-full bg-muted" />
                  {line}
                </li>
              ))}
            </ul>
          </div>
        </div>
      </ConfirmDialog>
    </SettingsPageShell>
  );
};
