import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  ArrowUpRight,
  Download,
  Hexagon,
  KeyRound,
  LayoutDashboard,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Terminal,
  WandSparkles,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

import { Button } from '../ui/button';
import { Badge } from '../ui/badge';
import { SettingsPageShell } from './SettingsPageShell';
import { SettingsResourceRow } from './SettingsPrimitives';
import { useApi } from '@/context/ApiContext';
import type { DependencyItem } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';

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
  node: { icon: Hexagon, tileCls: 'bg-violet-soft', iconCls: 'text-violet' },
};

export const SettingsDependenciesPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const [deps, setDeps] = useState<DependencyItem[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await api.listDependencies();
      setDeps(res.deps ?? []);
    } catch {
      setDeps([]);
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const install = async (dep: DependencyItem) => {
    setBusy(dep.id);
    try {
      const res = await api.installDependency(dep.id);
      showToast(
        res.ok
          ? t('settings.dependencies.installed', { name: t(`settings.dependencies.items.${dep.id}.label`) })
          : res.message || t('settings.dependencies.installFailed'),
        res.ok ? 'success' : 'error'
      );
      await refresh();
    } catch (e: any) {
      showToast(e?.message || t('settings.dependencies.installFailed'), 'error');
    } finally {
      setBusy(null);
    }
  };

  const statusText = (d: DependencyItem) => {
    if (!d.installed) return t('settings.dependencies.statusMissing');
    const word = d.kind === 'node' ? t('settings.dependencies.statusDetected') : t('settings.dependencies.statusReady');
    return d.version ? `${word} · v${String(d.version).replace(/^v/i, '')}` : word;
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
            const showAction = d.id === 'askill' || d.id === 'avault' || d.id === 'show-runtime';
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
                    <Badge variant={d.installed ? 'success' : 'destructive'} className="font-mono">
                      {statusText(d)}
                    </Badge>
                    {showAction && (
                      <Button variant={d.installed ? 'secondary' : 'brand'} size="xs" disabled={installing} onClick={() => void install(d)}>
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
                            ? d.id === 'show-runtime'
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
