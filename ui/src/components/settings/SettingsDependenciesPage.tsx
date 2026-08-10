import React, { useCallback, useEffect, useRef, useState } from 'react';
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
  dependenciesNeedAutomaticRefresh,
  dependencyHasInstallAction,
  dependencyIsStartupRepairing,
} from './SettingsDependenciesPage.logic';
import { errorMessage } from '@/lib/errorMessage';

// Mirrors design.pen "vibe-remote — Settings · Dependencies": one card per
// required local runtime (icon tile + name/REQUIRED + detail + status pill +
// action), reusing the Backends-page card shape. Startup reconciliation and
// `vibe runtime prepare` auto-install askill + the Show Page runtime; this page
// surfaces their status and offers manual re-check / install / repair. Backend CLIs are
// managed on the Backends tab — linked, not duplicated.

type DepMeta = { icon: LucideIcon; tileCls: string; iconCls: string };

const STARTUP_REFRESH_INTERVAL_MS = 1_500;

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
  const [reconciling, setReconciling] = useState(false);
  const [startupRepairingIds, setStartupRepairingIds] = useState<Set<string> | undefined>(undefined);
  const refreshSequence = useRef(0);

  const refresh = useCallback(async () => {
    const sequence = ++refreshSequence.current;
    try {
      const res = await api.listDependencies();
      if (sequence === refreshSequence.current) {
        setDeps(res.deps ?? []);
        setReconciling(Boolean(res.reconciling));
        setStartupRepairingIds(
          Array.isArray(res.reconciling_dependencies) ? new Set(res.reconciling_dependencies) : undefined,
        );
      }
      return res;
    } catch {
      if (sequence === refreshSequence.current) {
        setDeps([]);
        setReconciling(false);
        setStartupRepairingIds(undefined);
      }
      return null;
    }
  }, [api]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    let allowInitialRetry = true;

    const pollUntilSettled = async () => {
      const result = await refresh();
      const shouldRetry =
        !cancelled &&
        result !== null &&
        dependenciesNeedAutomaticRefresh(result, allowInitialRetry);
      // A single delayed retry catches the startup lock acquisition race. After
      // that first response, only an active backend reconciliation can poll.
      allowInitialRetry = false;
      if (
        shouldRetry
      ) {
        timer = window.setTimeout(() => void pollUntilSettled(), STARTUP_REFRESH_INTERVAL_MS);
      }
    };

    void pollUntilSettled();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [refresh]);

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

  const statusText = (d: DependencyItem) => {
    if (reconciling && dependencyIsStartupRepairing(d, startupRepairingIds)) {
      return t('settings.dependencies.installing');
    }
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
    if (reconciling && dependencyIsStartupRepairing(d, startupRepairingIds)) return 'warning';
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
            const startupInstalling = reconciling && dependencyIsStartupRepairing(d, startupRepairingIds);
            const showAction = dependencyHasInstallAction(d);
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
                    {d.id === 'memory-runtime' && d.installed && (
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
                        disabled={installing || startupInstalling}
                        onClick={() => void install(d)}
                      >
                        {installing || startupInstalling ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : d.installed ? (
                          <RefreshCw className="size-3.5" />
                        ) : (
                          <Download className="size-3.5" />
                        )}
                        {installing || startupInstalling
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
    </SettingsPageShell>
  );
};
