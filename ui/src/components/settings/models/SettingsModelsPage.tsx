// 设置 · 模型 — the Model Hub main page (design.pen 产品改造 V4 01r). Owns data
// fetching + the ordered source list; composes the 来源 band, Agent band,
// 最近切换 feed, and the 高级 row, plus the add-source dialogs. Talks to the hub
// through modelsApi (mock fixtures until L2's REST API is live — see
// featureFlags.ts).
import * as React from 'react';
import { CheckCircle2, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { useToast } from '@/context/ToastContext';
import { SettingsPageShell } from '../SettingsPageShell';
import { SourcesCard } from './SourcesCard';
import { AgentCard } from './AgentCard';
import { RecentSwitchesCard } from './RecentSwitchesCard';
import { AdvancedRow } from './AdvancedRow';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import { OAuthConnectDialog } from './OAuthConnectDialog';
import { modelsApi } from './modelsApi';
import type { AgentSupply, ResolutionEvent, RuntimeDependency, Source } from './types';

const StatusPill: React.FC<{ healthy: boolean; hubCount: number }> = ({ healthy, hubCount }) => {
  const { t } = useTranslation();
  return healthy ? (
    <Badge variant="success" className="gap-1.5 rounded-full px-3 py-1.5 text-[12px]">
      <CheckCircle2 className="size-3.5" />
      {t('settings.models.statusPill.ok', { count: hubCount })}
    </Badge>
  ) : (
    <Badge variant="warning" className="gap-1.5 rounded-full px-3 py-1.5 text-[12px]">
      <TriangleAlert className="size-3.5" />
      {t('settings.models.statusPill.degraded', { count: hubCount })}
    </Badge>
  );
};

export const SettingsModelsPage: React.FC = () => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [sources, setSources] = React.useState<Source[]>([]);
  const [agents, setAgents] = React.useState<AgentSupply[]>([]);
  const [events, setEvents] = React.useState<ResolutionEvent[]>([]);
  const [runtime, setRuntime] = React.useState<RuntimeDependency | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [connecting, setConnecting] = React.useState<string | null>(null);

  const [apiKeyOpen, setApiKeyOpen] = React.useState(false);
  const [oauthVendor, setOauthVendor] = React.useState<string | null>(null);

  // Mirror the latest ordered sources for reorder-commit (drag end reads the
  // freshest order without threading it through the framer callback).
  const sourcesRef = React.useRef<Source[]>(sources);
  sourcesRef.current = sources;

  React.useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([modelsApi.listSources(), modelsApi.listAgents(), modelsApi.listEvents(20), modelsApi.getRuntimeStatus()])
      .then(([s, a, e, r]) => {
        if (cancelled) return;
        setSources(s);
        setAgents(a);
        setEvents(e);
        setRuntime(r);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(err?.code || err?.message || 'load_failed');
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshSourcesAgents = React.useCallback(async () => {
    try {
      const [s, a] = await Promise.all([modelsApi.listSources(), modelsApi.listAgents()]);
      setSources(s);
      setAgents(a);
    } catch {
      /* keep the last good view */
    }
  }, []);

  const reorderPreview = (ids: string[]) => {
    setSources((prev) => {
      const byId = new Map(prev.map((s) => [s.id, s]));
      return ids.map((id) => byId.get(id)).filter((s): s is Source => Boolean(s));
    });
  };

  const reorderCommit = () => {
    const order = sourcesRef.current.map((s) => s.id);
    modelsApi
      .putPriority(order)
      .then((priority) => {
        // Re-echo the server's authoritative order.
        setSources((prev) => {
          const byId = new Map(prev.map((s) => [s.id, s]));
          return priority.order.map((id) => byId.get(id)).filter((s): s is Source => Boolean(s));
        });
      })
      .catch(() => {
        showToast(t('settings.models.toast.reorderFailed') as string, 'error');
        // The optimistic preview order diverged from the server; re-fetch so the
        // list reflects the persisted (unchanged) order rather than a phantom one.
        void refreshSourcesAgents();
      });
  };

  const connectHub = async (agent: AgentSupply) => {
    setConnecting(agent.backend);
    try {
      await modelsApi.setAgentMode(agent.backend, 'hub');
      await refreshSourcesAgents();
      showToast(t('settings.models.toast.connected') as string, 'success');
    } catch {
      showToast(t('settings.models.toast.connectFailed') as string, 'error');
    } finally {
      setConnecting(null);
    }
  };

  const hubCount = agents.filter((a) => a.mode === 'hub').length;
  // Only a fully-ok runtime + no errored source is "一切正常"; degraded / down /
  // not_installed / unknown all warrant the warning pill.
  const healthy = runtime?.status.health === 'ok' && !sources.some((s) => s.state.status === 'error');

  return (
    <SettingsPageShell
      activeTab="models"
      title={t('settings.models.title')}
      subtitle={t('settings.models.subtitle')}
      actions={!loading && !loadError ? <StatusPill healthy={healthy} hubCount={hubCount} /> : undefined}
    >
      {loading ? (
        <div className="text-[13px] text-muted">{t('common.loading')}</div>
      ) : loadError ? (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/[0.08] px-4 py-3 text-[13px] text-destructive">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" />
          <span>{t('settings.models.loadError', { detail: loadError })}</span>
        </div>
      ) : (
        <div className="flex flex-col gap-5">
          <SourcesCard
            sources={sources}
            onReorderPreview={reorderPreview}
            onReorderCommit={reorderCommit}
            onConnectClaude={() => setOauthVendor('anthropic')}
            onConnectChatGPT={() => setOauthVendor('openai')}
            onAddApiKey={() => setApiKeyOpen(true)}
          />
          <AgentCard agents={agents} sources={sources} onConnectHub={connectHub} connectingBackend={connecting} />
          <RecentSwitchesCard events={events} />
          <AdvancedRow />
        </div>
      )}

      <AddApiKeyDialog open={apiKeyOpen} onClose={() => setApiKeyOpen(false)} onAdded={() => void refreshSourcesAgents()} />
      <OAuthConnectDialog
        open={oauthVendor !== null}
        vendor={oauthVendor ?? 'anthropic'}
        onClose={() => setOauthVendor(null)}
        onConnected={() => void refreshSourcesAgents()}
      />
    </SettingsPageShell>
  );
};

export default SettingsModelsPage;
