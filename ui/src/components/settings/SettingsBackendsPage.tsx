import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ChevronRight, Settings2 } from 'lucide-react';

import { Button } from '../ui/button';
import { SettingsResourceRow, ToggleSwitch } from './SettingsPrimitives';
import { BackendLifecycleChip } from './BackendLifecycleChip';
import { SettingsPageShell } from './SettingsPageShell';
import { useApi } from '@/context/ApiContext';
import { useToast } from '@/context/ToastContext';
import { AGENT_BACKENDS, DEFAULT_AGENT_STATE } from '@/lib/agentBackends';

// Mirrors design.pen qVHh4 (VR/CM/Backends): three horizontal cards
// (OpenCode/Claude/Codex). Each card
// surfaces icon + name/description + status chip + enable toggle + a
// "Configure" link that drills into the level-2 provider page. CLI path,
// detect, install, and permission profile live on the provider page now —
// keep the level-1 page about backend availability, not Agent defaults.

type CliStatus = 'unknown' | 'ok' | 'missing';

type AgentState = {
  enabled: boolean;
  cli_path: string;
  status: CliStatus;
};

const DEFAULT_AGENTS = DEFAULT_AGENT_STATE as Record<string, AgentState>;

const normalizeAgents = (source: any): Record<string, AgentState> => {
  const raw = source?.agents || {};
  return Object.fromEntries(
    Object.entries(DEFAULT_AGENTS).map(([name, fallback]) => {
      const next = raw?.[name] || {};
      return [
        name,
        {
          enabled: typeof next.enabled === 'boolean' ? next.enabled : fallback.enabled,
          cli_path: next.cli_path || fallback.cli_path,
          status: (next.status as CliStatus) || fallback.status,
        },
      ];
    })
  );
};

export const SettingsBackendsPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();

  const [loaded, setLoaded] = useState(false);
  const [agents, setAgents] = useState<Record<string, AgentState>>(DEFAULT_AGENTS);

  useEffect(() => {
    let cancelled = false;
    api
      .getConfig()
      .then((config) => {
        if (cancelled) return;
        setAgents(normalizeAgents(config));
        setLoaded(true);
      })
      .catch(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  // Detect each CLI on mount so the status pill reflects reality without
  // making the user click Detect manually. Runs after the first config load.
  useEffect(() => {
    if (!loaded) return;
    let cancelled = false;
    (async () => {
      const results = await Promise.all(
        Object.entries(agents).map(async ([name, agent]) => {
          try {
            const result = await api.detectCli(agent.cli_path || name);
            return [name, result] as const;
          } catch {
            return [name, null] as const;
          }
        })
      );
      if (cancelled) return;
      setAgents((prev) => {
        const next = { ...prev };
        for (const [name, result] of results) {
          if (!result) continue;
          next[name] = {
            ...next[name],
            cli_path: result.path || next[name].cli_path,
            status: result.found ? 'ok' : 'missing',
          };
        }
        return next;
      });
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded]);

  const persist = async (nextAgents: Record<string, AgentState>) => {
    try {
      await api.saveConfig({
        agents: nextAgents,
      });
      showToast(t('common.saved'), 'success');
    } catch (e: any) {
      showToast(e?.message || t('common.saveFailed'), 'error');
    }
  };

  const handleToggle = async (name: string, enabled: boolean) => {
    const nextAgents = { ...agents, [name]: { ...agents[name], enabled } };
    setAgents(nextAgents);
    await persist(nextAgents);
  };

  const refreshDetectionFor = async (name: string, cli_path: string) => {
    try {
      const result = await api.detectCli(cli_path || name);
      setAgents((prev) => ({
        ...prev,
        [name]: {
          ...prev[name],
          cli_path: result.path || prev[name].cli_path,
          status: result.found ? 'ok' : 'missing',
        },
      }));
    } catch {
      // ignore — chip falls back to muted "loading" pill
    }
  };

  return (
    <SettingsPageShell
      activeTab="backends"
      title={t('settings.backendsTitle')}
      subtitle={t('settings.backendsSubtitle')}
    >
      {!loaded ? (
        <div className="text-sm text-muted">{t('common.loading')}</div>
      ) : (
        <div className="flex flex-col gap-3.5">
          {AGENT_BACKENDS.map((meta) => {
            const agent = agents[meta.id];
            const Icon = meta.Icon;
            const route = meta.settingsRoute;

            return (
              <SettingsResourceRow
                key={meta.id}
                icon={Icon}
                tileClassName={meta.tileCls}
                iconClassName={meta.iconCls}
                title={meta.label}
                detail={t(`settings.backends.${meta.id}Description`)}
                actions={
                  <>
                    <BackendLifecycleChip
                      name={meta.id}
                      enabled={agent.enabled}
                      cliStatus={agent.status}
                      onChanged={async (info) => {
                        const installedPath = info?.installedPath || null;
                        if (installedPath) {
                          setAgents((prev) => ({
                            ...prev,
                            [meta.id]: { ...prev[meta.id], cli_path: installedPath },
                          }));
                        }
                        await refreshDetectionFor(meta.id, installedPath || agent.cli_path);
                      }}
                    />
                    <ToggleSwitch
                      enabled={agent.enabled}
                      onClick={() => void handleToggle(meta.id, !agent.enabled)}
                    />
                    {route && (
                      <Button asChild variant="secondary" size="xs">
                        <Link to={route} aria-label={t('settings.backends.configure', { name: meta.label }) as string}>
                          <Settings2 className="size-3.5" />
                          {t('settings.backends.configure')}
                          <ChevronRight className="size-3.5" />
                        </Link>
                      </Button>
                    )}
                  </>
                }
              />
            );
          })}
        </div>
      )}
    </SettingsPageShell>
  );
};
