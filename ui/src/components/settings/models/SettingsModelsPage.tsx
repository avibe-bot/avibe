// Model-centric settings surface. The page reads model chains from the server,
// hosts shared source repair journeys, and serializes Agent writes by backend.
import * as React from 'react';
import { CheckCircle2, ListFilter, LoaderCircle, Play, TriangleAlert } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { useToast } from '@/context/ToastContext';
import { SettingsPageShell } from '../SettingsPageShell';
import { SourcesCard } from './SourcesCard';
import { AgentCard } from './AgentCard';
import { MigrationBanner } from './MigrationBanner';
import { RecentSwitchesCard } from './RecentSwitchesCard';
import { SourceOrderDrawer } from './SourceOrderDrawer';
import { AdvancedRow } from './AdvancedRow';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import { OAuthConnectDialog } from './OAuthConnectDialog';
import { RepairJourney, type RepairTarget } from './RepairJourney';
import {
  agentsWithEcho,
  createLatestAsyncAuthority,
  createPendingWrites,
  mapWithConcurrency,
  sourcesWithEcho,
} from './asyncLifetime';
import {
  emptyFeed,
  feedAfterHeadRead,
  feedAfterTailRead,
  feedTailCursor,
  type EventFeed,
} from './eventFeed';
import { AddCustomModelDialog } from './menus/AddCustomModelDialog';
import { OpenCodeMenuDrawer } from './menus/OpenCodeMenuDrawer';
import { modelsApi, type ModelsApi } from './modelsApi';
import { connectOutcome, isSupplyWarning } from './sufficiency';
import {
  manualModelSources,
  modelChainKey,
  modelChainRequests,
  modelIssueCount,
  routableMappings,
  type ModelChainIndex,
} from './modelRows';
import type { AgentBackend, AgentSupply, ResolutionEvent, RuntimeDependency, Source } from './types';

const ModelStatusButton: React.FC<{ issueCount: number; active: boolean; onClick: () => void }> = ({
  issueCount,
  active,
  onClick,
}) => {
  const { t } = useTranslation();
  const healthy = issueCount === 0;
  const Icon = healthy ? CheckCircle2 : TriangleAlert;
  return (
    <Button
      variant={healthy ? 'secondary' : 'outline'}
      size="sm"
      className={healthy ? 'h-9 text-mint' : 'h-9 border-destructive/35 text-destructive'}
      onClick={onClick}
      aria-pressed={active}
    >
      <Icon className="size-3.5" />
      {healthy ? t('settings.models.status.allHealthy') : t('settings.models.status.needsAction', { count: issueCount })}
      {!healthy && <ListFilter className="size-3.5" />}
    </Button>
  );
};

export const RuntimeNotStartedAction: React.FC<{ starting: boolean; onStart: () => void }> = ({
  starting,
  onStart,
}) => {
  const { t } = useTranslation();
  return (
    <div className="flex max-w-[calc(100vw-2rem)] flex-wrap items-center justify-end gap-2 text-[13px] text-muted sm:max-w-none">
      <span>{t('settings.models.runtime.notStarted')}</span>
      <Button variant="secondary" size="xs" onClick={onStart} disabled={starting}>
        {starting ? <LoaderCircle className="animate-spin" /> : <Play />}
        {t(starting ? 'settings.models.runtime.starting' : 'settings.models.runtime.startNow')}
      </Button>
    </div>
  );
};

export const ModelsPageActions: React.FC<{
  runtimeNotStarted: boolean;
  startingRuntime: boolean;
  issueCount: number;
  issuesOnly: boolean;
  onStartRuntime: () => void;
  onFocusIssues: () => void;
}> = ({
  runtimeNotStarted,
  startingRuntime,
  issueCount,
  issuesOnly,
  onStartRuntime,
  onFocusIssues,
}) => {
  if (!runtimeNotStarted) {
    return <ModelStatusButton issueCount={issueCount} active={issuesOnly} onClick={onFocusIssues} />;
  }
  return (
    <div className="flex max-w-[calc(100vw-2rem)] flex-wrap items-center justify-end gap-2 sm:max-w-none">
      {issueCount > 0 && (
        <ModelStatusButton issueCount={issueCount} active={issuesOnly} onClick={onFocusIssues} />
      )}
      <RuntimeNotStartedAction starting={startingRuntime} onStart={onStartRuntime} />
    </div>
  );
};

export async function startRuntimeWithStatusRefresh(
  api: Pick<ModelsApi, 'startRuntime' | 'getRuntimeStatus'>,
): Promise<{ runtime: RuntimeDependency | null; failed: boolean }> {
  try {
    return { runtime: await api.startRuntime(), failed: false };
  } catch {
    // A failed start changes supervisor health. Read that authoritative state
    // back so the persistent page does not keep presenting lazy-start idleness.
    const runtime = await api.getRuntimeStatus().catch(() => null);
    return { runtime, failed: true };
  }
}

export function pollRuntimeStatus(
  api: Pick<ModelsApi, 'getRuntimeStatus'>,
  onRuntime: (runtime: RuntimeDependency) => void,
  intervalMs = 5_000,
): () => void {
  let active = true;
  const refresh = async () => {
    try {
      const runtime = await api.getRuntimeStatus();
      if (active) onRuntime(runtime);
    } catch {
      // Keep the last authoritative snapshot and try again on the next tick.
    }
  };
  const interval = globalThis.setInterval(() => void refresh(), intervalMs);
  return () => {
    active = false;
    globalThis.clearInterval(interval);
  };
}

const CHAIN_READ_CONCURRENCY = 6;

const readModelChains = async (agents: AgentSupply[]): Promise<ModelChainIndex> => {
  const reads = await mapWithConcurrency(
    modelChainRequests(agents),
    CHAIN_READ_CONCURRENCY,
    async ({ backend, modelId }) => {
      const key = modelChainKey(backend, modelId);
      try {
        return [key, { kind: 'ready' as const, chain: await modelsApi.getAgentChain(backend, modelId) }] as const;
      } catch {
        return [key, { kind: 'error' as const }] as const;
      }
    },
  );
  return Object.fromEntries(reads);
};

const readModelSurface = async (): Promise<[Source[], AgentSupply[], ModelChainIndex]> => {
  const [sources, agents] = await Promise.all([modelsApi.listSources(), modelsApi.listAgents()]);
  return [sources, agents, await readModelChains(agents)];
};

type ModelSurfaceLanding =
  | {
      kind: 'surface';
      sources: Source[];
      agents: AgentSupply[];
      events: ResolutionEvent[] | null;
      chains: ModelChainIndex;
    }
  | { kind: 'source'; source: Source };

// 最近切换 is a cursor feed, not a fixed window: `/events` pages with `before`,
// so 「查看全部」 over one fetched page could never reach row 21. One page size for
// the first read and every 加载更早 read after it.
const EVENT_PAGE = 20;

export const SettingsModelsPage: React.FC = () => {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [sources, setSources] = React.useState<Source[]>([]);
  const [agents, setAgents] = React.useState<AgentSupply[]>([]);
  const [chains, setChains] = React.useState<ModelChainIndex>({});
  // Rows and end-of-feed as ONE value: every read moves both, and the transition
  // that moved only the rows is what made 加载更早 lie. `EventFeed` owns the rules.
  const [feed, setFeed] = React.useState<EventFeed>(emptyFeed);
  const [loadingEvents, setLoadingEvents] = React.useState(false);
  const [runtime, setRuntime] = React.useState<RuntimeDependency | null>(null);
  const [startingRuntime, setStartingRuntime] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [connecting, setConnecting] = React.useState<string | null>(null);
  const [refreshingSourceId, setRefreshingSourceId] = React.useState<string | null>(null);
  const [issuesOnly, setIssuesOnly] = React.useState(false);
  const agentSectionRef = React.useRef<HTMLDivElement>(null);

  const [apiKeyOpen, setApiKeyOpen] = React.useState(false);
  const [oauthVendor, setOauthVendor] = React.useState<string | null>(null);
  // The row + remedy a repair journey is running for. Holds the SOURCE OBJECT, not
  // its id, on purpose: a re-auth's first act is to change that row, so a live
  // lookup would rewrite the dialog's own subject mid-flow (RepairJourney.tsx).
  const [repairTarget, setRepairTarget] = React.useState<RepairTarget | null>(null);
  // Which backend's 模型菜单 / 来源顺序 drawer is open. Tracked by backend id (not
  // the agent object) so a background refresh keeps feeding the drawer the
  // freshest agent.
  const [menuBackend, setMenuBackend] = React.useState<AgentBackend | null>(null);
  const [orderBackend, setOrderBackend] = React.useState<AgentBackend | null>(null);
  const [customModelRequest, setCustomModelRequest] = React.useState<{
    sourceId?: string;
    backend?: AgentBackend;
  } | null>(null);

  // Which backends have a 来源顺序 write outstanding. Held HERE and not in the
  // drawer that issues it, because the drawer does not outlive its own write:
  // 完成, the close X, Escape and the overlay all stay live while the PUT and its
  // read-back are in flight, and closing unmounts the drawer, so a flag inside it
  // is re-created reading 「idle」 by the reopen — which is exactly when the
  // hand-off below must still be shut. See `createPendingWrites`.
  const [agentWrites, setAgentWrites] = React.useState<ReadonlySet<string>>(() => new Set());
  const [agentWriteRegistry] = React.useState(() => createPendingWrites(setAgentWrites));
  const agentsRef = React.useRef(agents);
  React.useEffect(() => {
    agentsRef.current = agents;
  }, [agents]);

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
    if (runtime?.status.health !== 'not_started' || startingRuntime) return undefined;
    return pollRuntimeStatus(modelsApi, (nextRuntime) => {
      if (aliveRef.current) setRuntime(nextRuntime);
    });
  }, [runtime?.status.health, startingRuntime]);

  // 最近切换 is re-read here with the rows, because the writes this refresh exists
  // for are the writes that FILE events: a failing 试跑 cools its head down through
  // `_cooldown`, which records the `cooldown` row that explains the state change
  // the user is about to see. Fetched only at mount, the row changed and the line
  // explaining it appeared nowhere until the page was reloaded — the explanation
  // arriving later than the thing it explains.
  // It rides along as an ANCILLARY leg, though — `null` when that one read failed.
  // The feed explains the rows; it is not the rows. Letting it reject the whole
  // `Promise.all` would mean a slow or broken `/events` holds back the repaired
  // source state and the ● 当前 the user just changed, which is the opposite of
  // this refresh's job. A feed left one write behind is not wrong, only not newer,
  // and the next mutation or reload catches it up.
  const [refreshAuthority] = React.useState(() =>
    createLatestAsyncAuthority<ModelSurfaceLanding>(
      (landing) => {
        if (!aliveRef.current) return;
        if (landing.kind === 'source') {
          setSources((previous) => sourcesWithEcho(previous, landing.source));
          return;
        }
        const { sources: nextSources, agents: nextAgents, events: headEvents, chains: nextChains } = landing;
        setSources(nextSources);
        setAgents(nextAgents);
        agentsRef.current = nextAgents;
        setChains(nextChains);
        // Merged, not replaced: 加载更早 pages tail-ward, and a head re-read must
        // not silently drop the rows it never asked for. See `feedAfterHeadRead`
        // for the one case merging is wrong, and for what that costs 加载更早.
        if (headEvents) setFeed((prev) => feedAfterHeadRead(prev, headEvents));
      },
    ),
  );

  React.useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all([readModelSurface(), modelsApi.listEvents(EVENT_PAGE), modelsApi.getRuntimeStatus()])
      .then(([[s, a, nextChains], e, r]) => {
        if (cancelled) return;
        setSources(s);
        setAgents(a);
        agentsRef.current = a;
        setChains(nextChains);
        // The first page is a tail read as much as a head one: it reaches the end
        // of the feed exactly when it comes back short, and its cursor is `null`
        // because it asked for the top. Applied to `prev` rather than to
        // `emptyFeed` so the same 「still the feed I asked about?」 rule covers it.
        setFeed((prev) => feedAfterTailRead(prev, e, EVENT_PAGE, null));
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
      await refreshAuthority.run(async () => {
        const [[nextSources, nextAgents, nextChains], headEvents] = await Promise.all([
          readModelSurface(),
          modelsApi.listEvents(EVENT_PAGE).catch(() => null),
        ]);
        return {
          kind: 'surface' as const,
          sources: nextSources,
          agents: nextAgents,
          events: headEvents,
          chains: nextChains,
        };
      });
    } catch {
      // A mutation may have succeeded server-side but the re-read failed — tell
      // the user the view might be stale rather than silently swallowing it.
      if (aliveRef.current) showToast(t('settings.models.toast.refreshFailed') as string, 'error');
    }
  }, [refreshAuthority, showToast, t]);

  /**
   * The one way an Agent write reports itself: hand back the row the server
   * echoed, then re-read everything else the write moved.
   *
   * Taking the echo is not an optimization of the re-read, it is the part that
   * cannot fail. `refreshSourcesAgents` swallows a failed read into a toast and
   * `refreshAuthority` drops a superseded one, so `await`ing it proves an attempt
   * finished and never that the page caught up — and the drawers seed, diff and
   * gate off these rows. `agentsWithEcho` explains why the echo is allowed to
   * speak for one; the re-read still runs because the echo is one Agent and the
   * write moved source rows, the other Agents and the feed too.
   */
  const agentSaved = React.useCallback(
    (echoed: AgentSupply) => {
      setAgents((prev) => {
        const next = agentsWithEcho(prev, echoed);
        agentsRef.current = next;
        return next;
      });
      return refreshSourcesAgents();
    },
    [refreshSourcesAgents],
  );

  const setModelRoute = React.useCallback(
    (
      backend: AgentBackend,
      modelId: string,
      targetModelId: string | null,
      onCommitted: (before: AgentSupply, after: AgentSupply) => void,
    ) => {
      void agentWriteRegistry.track(backend, async () => {
        const current = agentsRef.current.find((agent) => agent.backend === backend);
        if (!current || current.menu_kind !== 'fixed') return;
        const byModel = new Map(
          routableMappings(current, sources).map((mapping) => [mapping.builtin_id, mapping]),
        );
        if (targetModelId) {
          byModel.set(modelId, { builtin_id: modelId, target_model_id: targetModelId, enabled: true });
        } else {
          byModel.delete(modelId);
        }
        try {
          const echoed = await modelsApi.putMappings(backend, [...byModel.values()].filter((mapping) => mapping.enabled));
          onCommitted(current, echoed);
          await agentSaved(echoed);
        } catch {
          showToast(t('settings.models.menus.saveFailed') as string, 'error');
        }
      });
    },
    [agentSaved, agentWriteRegistry, showToast, sources, t],
  );

  const loadOlderEvents = React.useCallback(async () => {
    const oldest = feedTailCursor(feed);
    if (!oldest) return;
    setLoadingEvents(true);
    try {
      const page = await modelsApi.listEvents(EVENT_PAGE, oldest);
      if (!aliveRef.current) return;
      // Merged by id rather than concatenated: the feed grows at the head while
      // we page from the tail, so an overlapping row is normal, not a bug. Same
      // owner as the mount read, because reaching the end is the same question
      // there; the head re-read has its own because merging is not always right.
      //
      // The cursor goes back in with the page: this request is a question about
      // the rows below `oldest`, and a head re-read that REPLACED the feed while
      // it was in flight left it about a feed that is no longer on screen.
      setFeed((prev) => feedAfterTailRead(prev, page, EVENT_PAGE, oldest));
    } catch {
      if (aliveRef.current) showToast(t('settings.models.toast.refreshFailed') as string, 'error');
    } finally {
      if (aliveRef.current) setLoadingEvents(false);
    }
  }, [feed, showToast, t]);

  const openCustomModel = React.useCallback((sourceId?: string, backend?: AgentBackend) => {
    const writableSources = manualModelSources(sources);
    if (writableSources.length === 0) {
      setApiKeyOpen(true);
      return;
    }
    const requested = writableSources.find((source) => source.id === sourceId);
    setCustomModelRequest({ sourceId: requested?.id, backend });
  }, [sources]);

  const connectHub = async (agent: AgentSupply) => {
    setConnecting(agent.backend);
    try {
      // What the PATCH echo means is a rule, not an ad-hoc read of one field.
      // `connectOutcome` combines mode and supply status so the copy never
      // promises a Direct fallback the resolver does not perform.
      const echoed = await modelsApi.setAgentMode(agent.backend, 'hub');
      const outcome = connectOutcome(echoed, sources);
      await agentSaved(echoed);
      if (!aliveRef.current) return;
      if (outcome === 'failed') {
        showToast(t('settings.models.toast.connectFailed') as string, 'error');
      } else if (isSupplyWarning(outcome)) {
        showToast(t(`settings.models.supply.${outcome}`) as string, 'warning');
      } else {
        showToast(t('settings.models.toast.connected') as string, 'success');
      }
    } catch {
      if (aliveRef.current) showToast(t('settings.models.toast.connectFailed') as string, 'error');
    } finally {
      if (aliveRef.current) setConnecting(null);
    }
  };

  const refreshSource = async (source: Source) => {
    if (refreshingSourceId !== null) return;
    setRefreshingSourceId(source.id);
    try {
      // The mutation must fail outside the read authority: that authority
      // intentionally suppresses stale read errors, while a discovery failure
      // writes the source's error state and must always reach the honest toast.
      const refreshed = await modelsApi.refreshSource(source.id);
      await refreshAuthority.run(() =>
        Promise.resolve({ kind: 'source' as const, source: refreshed.source }),
      );
      await refreshSourcesAgents();
      if (aliveRef.current) {
        showToast(t('settings.models.sourceActions.refreshed', { count: refreshed.discovered }) as string, 'success');
      }
    } catch {
      if (!aliveRef.current) return;
      await refreshSourcesAgents();
      if (aliveRef.current) {
        showToast(t('settings.models.sourceActions.refreshFailed') as string, 'error');
      }
    } finally {
      if (aliveRef.current) setRefreshingSourceId(null);
    }
  };

  // Resolve an open drawer's agent from live state so edits see fresh data.
  const menuAgent = agents.find(
    (agent) => agent.backend === menuBackend && !agentWrites.has(agent.backend),
  ) ?? null;
  // AC-7: the 来源顺序 drawer exists for Hub-mode backends only. Gating here as
  // well as on the button means a mode flip while the drawer is open closes it,
  // instead of leaving an editor open over an order nothing reads.
  const orderAgent = agents.find((a) => a.backend === orderBackend && a.mode === 'hub') ?? null;

  const issueCount = modelIssueCount(agents, chains, runtime);
  const standardVendors = new Set(agents.flatMap((agent) => agent.standard_vendors ?? []));

  React.useEffect(() => {
    if (issueCount === 0) setIssuesOnly(false);
  }, [issueCount]);

  const focusIssues = () => {
    if (issueCount > 0) setIssuesOnly((value) => !value);
    else setIssuesOnly(false);
    requestAnimationFrame(() => agentSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  };

  const startRuntime = async () => {
    setStartingRuntime(true);
    try {
      const result = await startRuntimeWithStatusRefresh(modelsApi);
      if (!aliveRef.current) return;
      setRuntime(result.runtime);
      if (result.failed) showToast(t('settings.models.errors.startFailed') as string, 'error');
    } finally {
      if (aliveRef.current) setStartingRuntime(false);
    }
  };

  const runtimeNotStarted = runtime?.status.health === 'not_started';

  return (
    <SettingsPageShell
      activeTab="models"
      title={t('settings.models.title')}
      subtitle={t('settings.models.subtitle')}
      actions={
        !loading && !loadError ? (
          <ModelsPageActions
            runtimeNotStarted={runtimeNotStarted}
            startingRuntime={startingRuntime}
            issueCount={issueCount}
            issuesOnly={issuesOnly}
            onStartRuntime={() => void startRuntime()}
            onFocusIssues={focusIssues}
          />
        ) : undefined
      }
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
          <div ref={agentSectionRef} className="scroll-mt-6">
            <AgentCard
              agents={agents}
              sources={sources}
              chains={chains}
              runtime={runtime}
              issuesOnly={issuesOnly}
              pendingBackends={agentWrites}
              onConnectHub={(agent) => void connectHub(agent)}
              onOpenOrder={(agent) => setOrderBackend(agent.backend)}
              onOpenModels={(agent) => {
                if (!agentWrites.has(agent.backend)) setMenuBackend(agent.backend);
              }}
              onSetRoute={setModelRoute}
              onAddModel={(backend) => openCustomModel(undefined, backend)}
              onRepair={(source, kind) => setRepairTarget({ source, kind })}
              onRetest={(source) => void refreshSource(source)}
              retestingSourceId={refreshingSourceId}
              onProbeSettled={() => void refreshSourcesAgents()}
              connectingBackend={connecting}
            />
          </div>
          <SourcesCard
            sources={sources}
            onConnectClaude={() => setOauthVendor('anthropic')}
            onConnectChatGPT={() => setOauthVendor('openai')}
            onAddApiKey={() => setApiKeyOpen(true)}
            onSourceChanged={() => void refreshSourcesAgents()}
            onRefreshSource={(source) => void refreshSource(source)}
            refreshingSourceId={refreshingSourceId}
            onRepair={(source, kind) => setRepairTarget({ source, kind })}
            onAddModel={(source) => openCustomModel(source.id)}
          />
          <RecentSwitchesCard
            events={feed.events}
            sources={sources}
            hasMore={!feed.exhausted}
            loadingMore={loadingEvents}
            onLoadMore={loadOlderEvents}
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
      <RepairJourney
        target={repairTarget}
        onClose={() => setRepairTarget(null)}
        onChanged={() => void refreshSourcesAgents()}
      />

      {orderAgent && (
        <SourceOrderDrawer
          open
          agent={orderAgent}
          agents={agents}
          sources={sources}
          onClose={() => setOrderBackend(null)}
          // Returned, not discarded: the write stays marked pending for the whole
          // of this, so the hand-off to the menu drawer cannot open mid-write. What
          // makes the baseline it hands over CORRECT is the echo `agentSaved` takes
          // — the re-read is allowed to fail here without leaving one behind.
          onSaved={agentSaved}
          orderWrite={{
            pending: agentWrites.has(orderAgent.backend),
            track: (work) => agentWriteRegistry.track(orderAgent.backend, work),
          }}
        />
      )}

      {menuAgent && menuAgent.menu_kind === 'open' ? (
        <OpenCodeMenuDrawer
          open
          agent={menuAgent}
          sources={sources}
          onClose={() => setMenuBackend(null)}
          onSaved={(echoed) => void agentSaved(echoed)}
          // A custom model is a SOURCE write: it echoes the source, not the Agent.
          onRefresh={() => void refreshSourcesAgents()}
        />
      ) : null}

      <AddCustomModelDialog
        open={customModelRequest !== null}
        sources={sources}
        standardVendors={standardVendors}
        initialSourceId={customModelRequest?.sourceId}
        showOpenCodeIdentifier={customModelRequest?.backend === 'opencode'}
        onClose={() => setCustomModelRequest(null)}
        onSaved={() => void refreshSourcesAgents()}
      />
    </SettingsPageShell>
  );
};

export default SettingsModelsPage;
