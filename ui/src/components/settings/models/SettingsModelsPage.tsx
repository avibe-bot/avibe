// 设置 · 模型 — the Model Hub main page (design.pen 「产品改造 V6 01」, failover
// state 「V6 04」, mobile 「V6 M01」). Owns data fetching; composes the 来源 band,
// Agent band, 最近切换 feed and the 高级 row, plus the add-source dialogs, the
// per-Agent 来源顺序 drawer and the L5 menu drawers. Talks to the hub through
// modelsApi (mock fixtures until L2's REST API is live — see featureFlags.ts).
import * as React from 'react';
import { CheckCircle2, Info, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { useToast } from '@/context/ToastContext';
import { SettingsPageShell } from '../SettingsPageShell';
import { MODEL_MENUS_ENABLED } from './featureFlags';
import { SourcesCard } from './SourcesCard';
import { AgentCard } from './AgentCard';
import { MigrationBanner } from './MigrationBanner';
import { RecentSwitchesCard } from './RecentSwitchesCard';
import { SourceOrderDrawer } from './SourceOrderDrawer';
import { AdvancedRow } from './AdvancedRow';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import { OAuthConnectDialog } from './OAuthConnectDialog';
import { MappingDrawer } from './menus/MappingDrawer';
import { OpenCodeMenuDrawer } from './menus/OpenCodeMenuDrawer';
import { modelsApi } from './modelsApi';
import { connectOutcome, pageStatus, type PageStatus } from './supply';
import type { AgentBackend, AgentSupply, ResolutionEvent, RuntimeDependency, Source } from './types';

/**
 * The header pill — the one line that says whether the Hub is doing its job.
 *
 * V6 04 is why it can't be a boolean: a source ran out of quota, the chain covered
 * for it, and the honest headline is 「已自动切换，恢复后切回」 — a warning about
 * something already handled, which neither 一切正常 nor 需要处理 can express. The
 * ladder itself lives in supply.ts (a rule, unit-tested); this only renders it.
 */
const StatusPill: React.FC<{ status: PageStatus }> = ({ status }) => {
  const { t } = useTranslation();
  const k = `settings.models.statusPill.${status.kind}`;
  const text =
    status.kind === 'ok'
      ? (t(k, { count: status.hubCount }) as string)
      : status.kind === 'interrupted' || status.kind === 'waiting'
        ? (t(k, { count: status.count }) as string)
        : status.kind === 'needsAction' || status.kind === 'cooldown'
          ? [
              t(k, {
                source: status.source.display_name,
                // `detail_key` is required on needs_action / error but optional on
                // cooldown, so a missing one falls back to the status label rather
                // than interpolating an empty segment.
                detail: t(
                  status.source.state.detail_key ?? `settings.models.state.${status.source.state.status}`,
                ) as string,
              }) as string,
              // 「另有 N 个来源」 — one shared suffix instead of a per-branch
              // singular/plural pair. The pill names the worst source; the count
              // says the list has more of them.
              status.others > 0 ? (t('settings.models.statusPill.andMore', { count: status.others }) as string) : '',
            ]
              .filter(Boolean)
              .join(' · ')
          : (t(k) as string);

  const variant = status.tone === 'ok' ? 'success' : status.tone === 'warn' ? 'warning' : 'secondary';
  const Icon = status.tone === 'ok' ? CheckCircle2 : status.tone === 'warn' ? TriangleAlert : Info;
  return (
    <Badge variant={variant} className="gap-1.5 rounded-full px-3 py-1.5 text-[12px]">
      <Icon className="size-3.5" />
      {text}
    </Badge>
  );
};

// 最近切换 is a cursor feed, not a fixed window: `/events` pages with `before`,
// so 「查看全部」 over one fetched page could never reach row 21. One page size for
// the first read and every 加载更早 read after it.
const EVENT_PAGE = 20;

export const SettingsModelsPage: React.FC = () => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [sources, setSources] = React.useState<Source[]>([]);
  const [agents, setAgents] = React.useState<AgentSupply[]>([]);
  const [events, setEvents] = React.useState<ResolutionEvent[]>([]);
  // A short page is the end of the feed — the only end-of-list signal the
  // endpoint gives (there is no total).
  const [eventsExhausted, setEventsExhausted] = React.useState(true);
  const [loadingEvents, setLoadingEvents] = React.useState(false);
  const [runtime, setRuntime] = React.useState<RuntimeDependency | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [connecting, setConnecting] = React.useState<string | null>(null);

  const [apiKeyOpen, setApiKeyOpen] = React.useState(false);
  const [oauthVendor, setOauthVendor] = React.useState<string | null>(null);
  // Which backend's 模型菜单 / 来源顺序 drawer is open. Tracked by backend id (not
  // the agent object) so a background refresh keeps feeding the drawer the
  // freshest agent.
  const [menuBackend, setMenuBackend] = React.useState<AgentBackend | null>(null);
  const [orderBackend, setOrderBackend] = React.useState<AgentBackend | null>(null);

  // Guards event-handler async writes (refresh / connect) from landing after
  // the page unmounts — the whole class of stale-async writes the review flagged.
  //
  // The effect must re-arm the flag, not only clear it: an unmount-only cleanup
  // makes the guard one-way, and StrictMode's mount → cleanup → mount leaves it
  // false on a page that is very much alive. Every guarded write is then dropped
  // in silence — 查看更多 sticks on 加载中… forever because the `finally` that
  // clears it is guarded too. Found by clicking it in dev.
  const aliveRef = React.useRef(true);
  React.useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  React.useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([
      modelsApi.listSources(),
      modelsApi.listAgents(),
      modelsApi.listEvents(EVENT_PAGE),
      modelsApi.getRuntimeStatus(),
    ])
      .then(([s, a, e, r]) => {
        if (cancelled) return;
        setSources(s);
        setAgents(a);
        setEvents(e);
        setEventsExhausted(e.length < EVENT_PAGE);
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
      if (!aliveRef.current) return;
      setSources(s);
      setAgents(a);
    } catch {
      // A mutation may have succeeded server-side but the re-read failed — tell
      // the user the view might be stale rather than silently swallowing it.
      if (aliveRef.current) showToast(t('settings.models.toast.refreshFailed') as string, 'error');
    }
  }, [showToast, t]);

  const loadOlderEvents = React.useCallback(async () => {
    const oldest = events[events.length - 1]?.id;
    if (!oldest) return;
    setLoadingEvents(true);
    try {
      const page = await modelsApi.listEvents(EVENT_PAGE, oldest);
      if (!aliveRef.current) return;
      // Merged by id rather than concatenated: the feed grows at the head while
      // we page from the tail, so an overlapping row is normal, not a bug.
      setEvents((prev) => {
        const seen = new Set(prev.map((e) => e.id));
        return [...prev, ...page.filter((e) => !seen.has(e.id))];
      });
      setEventsExhausted(page.length < EVENT_PAGE);
    } catch {
      if (aliveRef.current) showToast(t('settings.models.toast.refreshFailed') as string, 'error');
    } finally {
      if (aliveRef.current) setLoadingEvents(false);
    }
  }, [events, showToast, t]);

  const connectHub = async (agent: AgentSupply) => {
    setConnecting(agent.backend);
    try {
      // What the PATCH echo means is a rule, not an ad-hoc read of one field —
      // see connectOutcome, which exists because `current: null` conflates four
      // unrelated states and the copy behind it promised a Direct fallback the
      // resolver does not perform.
      const outcome = connectOutcome(await modelsApi.setAgentMode(agent.backend, 'hub'));
      await refreshSourcesAgents();
      if (!aliveRef.current) return;
      if (outcome === 'failed') {
        showToast(t('settings.models.toast.connectFailed') as string, 'error');
      } else if (outcome === 'connected') {
        showToast(t('settings.models.toast.connected') as string, 'success');
      } else {
        showToast(t(`settings.models.supply.${outcome}`) as string, 'warning');
      }
    } catch {
      if (aliveRef.current) showToast(t('settings.models.toast.connectFailed') as string, 'error');
    } finally {
      if (aliveRef.current) setConnecting(null);
    }
  };

  // Resolve an open drawer's agent from live state so edits see fresh data.
  const menuAgent = agents.find((a) => a.backend === menuBackend) ?? null;
  // AC-7: the 来源顺序 drawer exists for Hub-mode backends only. Gating here as
  // well as on the button means a mode flip while the drawer is open closes it,
  // instead of leaving an editor open over an order nothing reads.
  const orderAgent = agents.find((a) => a.backend === orderBackend && a.mode === 'hub') ?? null;

  const status = pageStatus(sources, agents, runtime);

  return (
    <SettingsPageShell
      activeTab="models"
      title={t('settings.models.title')}
      subtitle={t('settings.models.subtitle')}
      actions={!loading && !loadError ? <StatusPill status={status} /> : undefined}
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
          <MigrationBanner onApplied={() => void refreshSourcesAgents()} />
          <SourcesCard
            sources={sources}
            onConnectClaude={() => setOauthVendor('anthropic')}
            onConnectChatGPT={() => setOauthVendor('openai')}
            onAddApiKey={() => setApiKeyOpen(true)}
            onSourceChanged={() => void refreshSourcesAgents()}
          />
          <AgentCard
            agents={agents}
            sources={sources}
            onConnectHub={connectHub}
            onOpenOrder={(agent) => setOrderBackend(agent.backend)}
            connectingBackend={connecting}
          />
          <RecentSwitchesCard
            events={events}
            sources={sources}
            hasMore={!eventsExhausted}
            loadingMore={loadingEvents}
            onLoadMore={() => void loadOlderEvents()}
          />
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

      {orderAgent && (
        <SourceOrderDrawer
          open
          agent={orderAgent}
          agents={agents}
          sources={sources}
          onClose={() => setOrderBackend(null)}
          onSaved={() => void refreshSourcesAgents()}
          // 模型菜单与映射 hands off to the menu drawer: the two answer adjacent
          // questions (which sources, which models), and V6 02's footer is the only
          // way into the menu now that the row's action is 来源顺序. Withheld while
          // the menus are flagged off, rather than opening onto nothing.
          onOpenMenu={
            MODEL_MENUS_ENABLED
              ? () => {
                  setOrderBackend(null);
                  setMenuBackend(orderAgent.backend);
                }
              : undefined
          }
        />
      )}

      {menuAgent && menuAgent.menu_kind === 'open' ? (
        <OpenCodeMenuDrawer
          open
          agent={menuAgent}
          sources={sources}
          onClose={() => setMenuBackend(null)}
          onSaved={() => void refreshSourcesAgents()}
          onRefresh={() => void refreshSourcesAgents()}
        />
      ) : menuAgent && (menuAgent.backend === 'claude' || menuAgent.backend === 'codex') ? (
        <MappingDrawer
          open
          backend={menuAgent.backend}
          agent={menuAgent}
          sources={sources}
          onClose={() => setMenuBackend(null)}
          onSaved={() => void refreshSourcesAgents()}
        />
      ) : null}
    </SettingsPageShell>
  );
};

export default SettingsModelsPage;
