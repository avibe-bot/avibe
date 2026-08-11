import * as React from 'react';
import { ArrowLeft, Gauge, Info, LoaderCircle, Route } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { useToast } from '@/context/ToastContext';
import { cn } from '@/lib/utils';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import { AdvancedRow } from './AdvancedRow';
import { EnableGatewayDialog } from './EnableGatewayDialog';
import { GatewayModule } from './GatewayModule';
import { InstallGatewayDialog } from './InstallGatewayDialog';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import { RecentSwitchesCard } from './RecentSwitchesCard';
import { RouteChainDialog } from './RouteChainDialog';
import { SourceDetailPanel } from './SourceDetailPanel';
import { SourceOrderDrawer } from './SourceOrderDrawer';
import { SourcesCard } from './SourcesCard';
import { modelsSurfaceKind } from './modelHubSurfaceState';
import { buildSupplyRelations } from './supplyRelations';
import { SupplyGraph, SupplyLegend } from './SupplyGraph';
import './modelHubSurface.css';
import { agentsWithEcho, createLatestAsyncAuthority, createPendingWrites, mapWithConcurrency } from './asyncLifetime';
import { emptyFeed, feedAfterHeadRead, feedAfterTailRead, feedTailCursor, type EventFeed } from './eventFeed';
import { modelsApi, type SourceCreated } from './modelsApi';
import { modelChainKey, modelChainRequests, type ModelChainIndex } from './modelRows';
import { pollRuntimeStatus, startRuntimeWithStatusRefresh } from './runtimeLifecycle';
import { backendVisual } from './vendorMeta';
import type { AdoptedBy, AgentBackend, AgentSupply, ResolutionEvent, RuntimeDependency, Source } from './types';

const CHAIN_READ_CONCURRENCY = 6;
const EVENT_PAGE = 20;

const readChains = async (agents: AgentSupply[]): Promise<ModelChainIndex> => Object.fromEntries(
  await mapWithConcurrency(
    modelChainRequests(agents.filter((agent) => agent.mode === 'hub')),
    CHAIN_READ_CONCURRENCY,
    async ({ backend, modelId }) => {
      const key = modelChainKey(backend, modelId);
      try {
        return [key, { kind: 'ready' as const, chain: await modelsApi.getAgentChain(backend, modelId) }] as const;
      } catch {
        return [key, { kind: 'error' as const }] as const;
      }
    },
  ),
);

type SurfaceLanding = {
  sources: Source[] | null;
  agents: AgentSupply[] | null;
  runtime: RuntimeDependency | null;
  chains: ModelChainIndex | null;
  events: ResolutionEvent[] | null;
  failed: boolean;
};

const readSurfaceLanding = async (): Promise<SurfaceLanding> => {
  const [sources, agents, runtime, events] = await Promise.allSettled([
    modelsApi.listSources(),
    modelsApi.listAgents(),
    modelsApi.getRuntimeStatus(),
    modelsApi.listEvents(EVENT_PAGE),
  ]);
  const agentRows = agents.status === 'fulfilled' ? agents.value : null;
  return {
    sources: sources.status === 'fulfilled' ? sources.value : null,
    agents: agentRows,
    runtime: runtime.status === 'fulfilled' ? runtime.value : null,
    chains: agentRows ? await readChains(agentRows) : null,
    events: events.status === 'fulfilled' ? events.value : null,
    failed: sources.status === 'rejected' || agents.status === 'rejected' || runtime.status === 'rejected' || events.status === 'rejected',
  };
};

type PageReadState = 'loading' | 'ready' | 'error';

export const RuntimePill: React.FC<{
  runtime: RuntimeDependency | null;
  statusUnread: boolean;
  starting: boolean;
  onStart: () => void;
  onInstall: () => void;
}> = ({ runtime, statusUnread, starting, onStart, onInstall }) => {
  const { t } = useTranslation();
  const health = statusUnread ? 'down' : runtime?.status.health ?? 'down';
  const canInstall = health === 'not_installed' && Boolean(runtime?.manifest.assets.length);
  const key = starting
    ? 'starting'
    : health === 'installing'
      ? 'starting'
    : health === 'ok'
      ? 'running'
      : health === 'degraded'
        ? 'degraded'
        : health === 'down'
          ? 'stopped'
          : health === 'not_started'
            ? 'notStarted'
            : canInstall
              ? 'notInstalled'
              : 'unsupported';
  const action = !starting && health !== 'installing' && (health === 'down' || health === 'not_started')
    ? onStart
    : !starting && canInstall
      ? onInstall
      : null;
  const className = cn(
    'model-hub-runtime-pill',
    (health === 'down' || health === 'degraded') && 'model-hub-runtime-pill--error',
  );
  const content = <><span className="model-hub-runtime-dot" />{(starting || health === 'installing') && <LoaderCircle className="animate-spin" />}{t(`settings.models.shell.${key}`)}</>;
  return action
    ? <button type="button" className={className} onClick={action}>{content}</button>
    : <span className={className}>{content}</span>;
};

const ModelHubShell: React.FC<{ actions?: React.ReactNode; detailBack?: () => void; children: React.ReactNode }> = ({ actions, detailBack, children }) => {
  const { t } = useTranslation();
  return (
    <div className="model-hub-shell">
      <header className="model-hub-shell-head">
        {detailBack
          ? <button type="button" onClick={detailBack} aria-label={t('settings.models.sourceDetail.back') as string} title={t('settings.models.sourceDetail.back') as string} className="model-hub-detail-back"><ArrowLeft aria-hidden="true" /></button>
          : <span className="flex items-center gap-[9px]">
              <h1>{t('settings.models.shell.title')}</h1>
              <ModelHubInfoHint
                label={t('settings.models.shell.gatewayInfo.label')}
                content={t('settings.models.shell.gatewayInfo.body')}
                className="model-hub-shell-info"
              />
            </span>}
        {actions}
      </header>
      {children}
    </div>
  );
};

const HubTabs: React.FC<{ tab: 'sources' | 'usage'; onChange: (tab: 'sources' | 'usage') => void }> = ({ tab, onChange }) => {
  const { t } = useTranslation();
  return (
    <div role="tablist" className="flex h-[39px] items-end gap-1 border-b border-border">
      {(['sources', 'usage'] as const).map((id) => (
        <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => onChange(id)} className={cn('flex h-[39px] items-center gap-[7px] border-b-2 px-3.5 text-[13px] transition-colors', tab === id ? 'border-mint font-semibold text-foreground' : 'border-transparent font-normal text-muted hover:text-foreground')}>
          {id === 'sources' ? <Route className="size-3.5" /> : <Gauge className="size-3.5" />}
          {t(`settings.models.shell.tab.${id === 'sources' ? 'hub' : 'usage'}`)}
        </button>
      ))}
    </div>
  );
};

const DirectHome: React.FC<{ agents: AgentSupply[]; onSwitch: (agent: AgentSupply) => void }> = ({ agents, onSwitch }) => {
  const { t } = useTranslation();
  if (agents.length === 0) {
    return <section className="model-hub-direct-empty"><h2>{t('settings.models.direct.empty.title')}</h2><p>{t('settings.models.direct.empty.body')}</p><span>{t('settings.models.direct.empty.install')}</span></section>;
  }
  return (
    <div className="model-hub-direct">
      <div className="model-hub-direct-grid grid">
        <section className="model-hub-direct-card overflow-hidden border border-border bg-surface">
          <div className="model-hub-direct-head flex items-center justify-between gap-3 border-b border-border px-5 py-3">
            <div><h2 className="text-[16px] font-bold text-foreground">{t('settings.models.direct.card.current')}</h2><p className="mt-1 text-[11.5px] text-muted">{t('settings.models.direct.card.current.sub')}</p></div>
            <span className="model-hub-direct-kind-pill rounded-full border px-2.5 py-1 text-[10px] font-semibold">{t('settings.models.direct.pill.direct')}</span>
          </div>
          <div className="model-hub-direct-content flex flex-col">
            {agents.map((agent) => {
              const { Icon } = backendVisual(agent.backend);
              return (
                <div key={agent.backend} className="model-hub-direct-row flex flex-col gap-2.5 bg-background px-3 py-3 sm:h-16 sm:flex-row sm:items-center sm:justify-between sm:py-0">
                  <div className="flex min-w-0 items-center gap-2.5">
                    <span className="model-hub-direct-tile flex size-[34px] shrink-0 items-center justify-center rounded-[9px]"><Icon className="size-[17px]" /></span>
                    <span><span className="block text-[13.5px] font-semibold text-foreground">{t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend })}</span><span className="mt-0.5 block truncate text-[11px] text-muted" title={t(`settings.models.direct.backend.${agent.backend}.detail`) as string}>{t(`settings.models.direct.backend.${agent.backend}.detail`)}</span></span>
                  </div>
                  <Button variant="secondary" size="sm" className="h-auto rounded-lg px-3 py-[9px] text-[11.5px] font-bold" onClick={() => onSwitch(agent)}>{t('settings.models.direct.action.switchToGateway')}</Button>
                </div>
              );
            })}
          </div>
        </section>
        <section className="model-hub-direct-card overflow-hidden border border-border bg-surface">
          <div className="model-hub-direct-head flex items-center border-b border-border px-5 py-3"><h2 className="text-[16px] font-bold text-foreground">{t('settings.models.direct.benefits.title')}</h2></div>
          <div className="model-hub-direct-content flex flex-col">{(['1', '2', '3'] as const).map((key) => <div key={key} className="model-hub-direct-row flex gap-2.5 bg-background px-3 py-[11px]"><span className="model-hub-ink-mint grid size-5 shrink-0 place-items-center rounded-full bg-mint-soft text-[10.5px] font-bold">{key}</span><span><span className="block text-[12.5px] font-semibold text-foreground">{t(`settings.models.direct.benefits.${key}`)}</span><span className="mt-1 block text-[11px] leading-relaxed text-muted">{t(`settings.models.direct.benefits.${key}.detail`)}</span></span></div>)}</div>
        </section>
      </div>
      <p className="model-hub-direct-note flex items-center justify-center border text-center text-[11.5px]">{t('settings.models.direct.note.perBackend')}</p>
    </div>
  );
};

const DirectPill: React.FC<{ count: number }> = ({ count }) => {
  const { t } = useTranslation();
  if (count === 0) return null;
  return <span className="model-hub-runtime-pill model-hub-runtime-pill--direct"><span className="model-hub-runtime-dot" />{t('settings.models.shell.allDirect', { count })}</span>;
};

const TakeoverPill: React.FC<{ count: number }> = ({ count }) => {
  const { t } = useTranslation();
  if (count === 0) return null;
  return <span className="model-hub-takeover-pill"><span className="model-hub-runtime-dot" />{t('settings.models.takeover.pill', { count })}</span>;
};

export const SettingsModelsPage: React.FC = () => {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const [sources, setSources] = React.useState<Source[]>([]);
  const [agents, setAgents] = React.useState<AgentSupply[]>([]);
  const [chains, setChains] = React.useState<ModelChainIndex>({});
  const [runtime, setRuntime] = React.useState<RuntimeDependency | null>(null);
  const [feed, setFeed] = React.useState<EventFeed>(emptyFeed);
  const [loadingEvents, setLoadingEvents] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [sourcesState, setSourcesState] = React.useState<PageReadState>('loading');
  const [agentsState, setAgentsState] = React.useState<PageReadState>('loading');
  const [runtimeState, setRuntimeState] = React.useState<PageReadState>('loading');
  const [eventsState, setEventsState] = React.useState<PageReadState>('loading');
  const [tab, setTab] = React.useState<'sources' | 'usage'>('sources');
  const [startingRuntime, setStartingRuntime] = React.useState(false);
  const [runtimeRecoveryPending, setRuntimeRecoveryPending] = React.useState(false);
  const [installOpen, setInstallOpen] = React.useState(false);
  const [apiKeyOpen, setApiKeyOpen] = React.useState(false);
  const [orderBackend, setOrderBackend] = React.useState<AgentBackend | null>(null);
  const [adoptAgent, setAdoptAgent] = React.useState<AgentSupply | null>(null);
  const [routeTarget, setRouteTarget] = React.useState<{ backend: AgentBackend; modelId: string } | null>(null);
  const [selectedSourceId, setSelectedSourceId] = React.useState<string | null>(null);
  const [adoptionBySource, setAdoptionBySource] = React.useState<Readonly<Record<string, readonly AdoptedBy[]>>>({});
  const [agentWrites, setAgentWrites] = React.useState<ReadonlySet<string>>(() => new Set());
  const [switchFailures, setSwitchFailures] = React.useState<ReadonlySet<string>>(() => new Set());
  const [agentWriteRegistry] = React.useState(() => createPendingWrites(setAgentWrites));
  const overviewRef = React.useRef<HTMLDivElement>(null);
  const aliveRef = React.useRef(true);
  const eventsHaveSnapshotRef = React.useRef(false);
  React.useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const runtimeHealth = runtime?.status.health ?? null;
  React.useEffect(() => {
    const runtimeCanRecover = runtimeRecoveryPending
      || runtimeHealth === 'not_started'
      || runtimeHealth === 'not_installed'
      || runtimeHealth === 'installing'
      || runtimeHealth === 'down'
      || runtimeHealth === 'degraded';
    if (!runtimeCanRecover || startingRuntime) return undefined;
    return pollRuntimeStatus(modelsApi, (nextRuntime) => {
      if (!aliveRef.current) return;
      setRuntime(nextRuntime);
      setRuntimeState('ready');
      setRuntimeRecoveryPending(false);
    });
  }, [runtimeHealth, runtimeRecoveryPending, startingRuntime]);

  const [refreshAuthority] = React.useState(() => createLatestAsyncAuthority<SurfaceLanding>((landing) => {
    if (!aliveRef.current) return;
    if (landing.sources !== null) {
      setSources(landing.sources);
      setSourcesState('ready');
    } else setSourcesState('error');
    if (landing.agents !== null) {
      setAgents(landing.agents);
      setAgentsState('ready');
    } else setAgentsState('error');
    if (landing.runtime !== null) {
      setRuntime(landing.runtime);
      setRuntimeState('ready');
      setRuntimeRecoveryPending(false);
    } else setRuntimeState('error');
    if (landing.chains !== null) setChains(landing.chains);
    if (landing.events !== null) {
      const events = landing.events;
      const hadSnapshot = eventsHaveSnapshotRef.current;
      eventsHaveSnapshotRef.current = true;
      setFeed((previous) => hadSnapshot
        ? feedAfterHeadRead(previous, events)
        : feedAfterTailRead(previous, events, EVENT_PAGE, null));
      setEventsState('ready');
    } else setEventsState('error');
  }));

  const refresh = React.useCallback(async () => {
    const outcome: { landing: SurfaceLanding | null } = { landing: null };
    await refreshAuthority.run(async () => {
      outcome.landing = await readSurfaceLanding();
      return outcome.landing;
    });
    if (aliveRef.current && outcome.landing?.failed) {
      showToast(t('settings.models.toast.refreshFailed') as string, 'error');
    }
  }, [refreshAuthority, showToast, t]);
  React.useEffect(() => {
    let active = true;
    void readSurfaceLanding().then((landing) => {
      if (!active) return;
      if (landing.sources !== null) {
        setSources(landing.sources);
        setSourcesState('ready');
      } else {
        setSourcesState('error');
      }
      if (landing.agents !== null) {
        setAgents(landing.agents);
        setAgentsState('ready');
      } else {
        setAgentsState('error');
      }
      if (landing.runtime !== null) {
        setRuntime(landing.runtime);
        setRuntimeState('ready');
      } else {
        setRuntimeState('error');
      }
      if (landing.chains !== null) setChains(landing.chains);
      if (landing.events !== null) {
        const events = landing.events;
        setFeed((previous) => feedAfterTailRead(previous, events, EVENT_PAGE, null));
        eventsHaveSnapshotRef.current = true;
        setEventsState('ready');
      } else setEventsState('error');
      setLoading(false);
    });
    return () => { active = false; };
  }, []);

  const retrySources = React.useCallback(async () => {
    setSourcesState('loading');
    await refresh();
  }, [refresh]);

  const retryAgents = React.useCallback(async () => {
    setAgentsState('loading');
    await refresh();
  }, [refresh]);

  const retryEvents = React.useCallback(async () => {
    setEventsState('loading');
    await refresh();
  }, [refresh]);

  const refreshAgentChains = React.useCallback(async (agent: AgentSupply) => {
    const requests = modelChainRequests([agent]);
    setChains((previous) => ({
      ...previous,
      ...Object.fromEntries(requests.map(({ backend, modelId }) => [modelChainKey(backend, modelId), { kind: 'loading' as const }])),
    }));
    const next = await readChains([agent]);
    if (aliveRef.current) setChains((previous) => ({ ...previous, ...next }));
  }, []);

  const agentSaved = React.useCallback((echoed: AgentSupply) => {
    setAgents((previous) => agentsWithEcho(previous, echoed));
    return refresh();
  }, [refresh]);

  const switchToDirect = (agent: AgentSupply) => {
    setSwitchFailures((previous) => {
      const next = new Set(previous);
      next.delete(agent.backend);
      return next;
    });
    void agentWriteRegistry.track(agent.backend, async () => {
      try {
        const echoed = await modelsApi.setAgentMode(agent.backend, 'direct');
        setSwitchFailures((previous) => {
          const next = new Set(previous);
          next.delete(agent.backend);
          return next;
        });
        setAdoptAgent(null);
        await agentSaved(echoed);
      } catch {
        setSwitchFailures((previous) => new Set(previous).add(agent.backend));
      }
    });
  };
  const loadOlderEvents = React.useCallback(async () => {
    const cursor = feedTailCursor(feed);
    if (!cursor) return;
    setLoadingEvents(true);
    try {
      const events = await modelsApi.listEvents(EVENT_PAGE, cursor);
      if (aliveRef.current) setFeed((previous) => feedAfterTailRead(previous, events, EVENT_PAGE, cursor));
    } catch {
      if (aliveRef.current) showToast(t('settings.models.toast.refreshFailed') as string, 'error');
    } finally {
      if (aliveRef.current) setLoadingEvents(false);
    }
  }, [feed, showToast, t]);
  const startRuntime = async () => {
    if (startingRuntime) return;
    setStartingRuntime(true);
    try {
      const result = await startRuntimeWithStatusRefresh(modelsApi);
      setRuntime(result.runtime);
      setRuntimeState(result.runtime === null ? 'error' : 'ready');
      setRuntimeRecoveryPending(result.runtime === null);
      if (result.failed) showToast(t('settings.models.errors.startFailed') as string, 'error');
    } finally {
      setStartingRuntime(false);
    }
  };
  const directEmpty = sourcesState === 'ready'
    && agentsState === 'ready'
    && modelsSurfaceKind(agents, sources) === 'direct_empty';
  const selectedSource = sources.find((source) => source.id === selectedSourceId) ?? null;
  const orderAgent = agents.find((agent) => agent.backend === orderBackend && agent.mode === 'hub') ?? null;
  const routeAgent = agents.find((agent) => agent.backend === routeTarget?.backend) ?? null;
  const routeSelection = routeTarget && routeAgent ? {
    agent: routeAgent,
    modelId: routeTarget.modelId,
    read: chains[modelChainKey(routeTarget.backend, routeTarget.modelId)],
  } : null;
  const supplyRelations = React.useMemo(() => buildSupplyRelations(agents, sources, chains), [agents, sources, chains]);
  const takeoverCount = React.useMemo(() => new Set(
    supplyRelations.filter(({ kind }) => kind === 'takeover').map(({ backend }) => backend),
  ).size, [supplyRelations]);
  const sourceAdded = async (created: SourceCreated) => {
    setAdoptionBySource((previous) => ({ ...previous, [created.source.id]: created.adopted_by }));
    setSources((previous) => [created.source, ...previous.filter((source) => source.id !== created.source.id)]);
    setSourcesState('ready');
    setApiKeyOpen(false);
    await refresh();
    setSelectedSourceId(created.source.id);
  };

  return (
    <ModelHubShell
      detailBack={selectedSourceId ? () => setSelectedSourceId(null) : undefined}
      actions={!loading
        ? directEmpty
          ? <DirectPill count={agents.length} />
          : <span className="flex items-center gap-2">
              <RuntimePill
                runtime={runtime}
                statusUnread={runtimeState === 'error' || runtimeRecoveryPending}
                starting={startingRuntime}
                onStart={() => void startRuntime()}
                onInstall={() => setInstallOpen(true)}
              />
              <TakeoverPill count={takeoverCount} />
            </span>
        : undefined}
    >
      {loading ? <div className="text-[13px] text-muted">{t('common.loading')}</div>
        : selectedSourceId
          ? selectedSource
            ? <SourceDetailPanel source={selectedSource} adoptedBy={adoptionBySource[selectedSource.id]} onChanged={refresh} />
            : <section className="rounded-xl border border-border bg-surface px-5 py-12 text-center text-[12px] text-muted">{t('settings.models.sourceDetail.gone')}</section>
          : directEmpty ? <DirectHome agents={agents} onSwitch={setAdoptAgent} />
            : <div className="space-y-[22px]">
                  <HubTabs tab={tab} onChange={setTab} />
                  {tab === 'sources' ? <div className="model-hub-overview">
                    <div className="model-hub-overview-body">
                      <div ref={overviewRef} className="model-hub-overview-grid relative flex flex-col gap-4">
                        <SourcesCard sources={sources} adoptionBySource={adoptionBySource} readState={sourcesState} onRetry={() => void retrySources()} onOpenSource={(source) => setSelectedSourceId(source.id)} onAddApiKey={() => setApiKeyOpen(true)} />
                        <div className="hidden lg:block" aria-hidden="true" />
                        <GatewayModule agents={agents} sources={sources} chains={chains} runtime={runtime} readState={agentsState} onRetry={() => void retryAgents()} pendingBackends={agentWrites} switchFailures={switchFailures} connectingBackend={adoptAgent?.backend ?? null} onConnectHub={setAdoptAgent} onSwitchDirect={switchToDirect} onOpenOrder={(agent) => setOrderBackend(agent.backend)} onOpenRoute={(agent, modelId) => setRouteTarget({ backend: agent.backend, modelId })} onProbeSettled={(agent) => void refreshAgentChains(agent)} />
                        <SupplyGraph containerRef={overviewRef} relations={supplyRelations} />
                      </div>
                      <SupplyLegend relations={supplyRelations} />
                    </div>
                    <RecentSwitchesCard events={feed.events} sources={sources} readState={eventsState} sourcesRead={sourcesState === 'ready'} onRetry={retryEvents} hasMore={!feed.exhausted} loadingMore={loadingEvents} onLoadMore={loadOlderEvents} />
                    <AdvancedRow />
                  </div> : <section className="rounded-xl border border-border bg-surface px-5 py-8"><div className="flex items-start gap-3"><Info className="mt-0.5 size-4 text-muted" /><div><h2 className="text-[14px] font-semibold text-foreground">{t('settings.models.usageTab.title')}</h2><p className="mt-1 text-[12px] text-muted">{t('settings.models.usageTab.detail')}</p></div></div></section>}
                </div>}
      <AddApiKeyDialog open={apiKeyOpen} onClose={() => setApiKeyOpen(false)} onAdded={(created) => void sourceAdded(created)} />
      {orderAgent && <SourceOrderDrawer open agent={orderAgent} sources={sources} onClose={() => setOrderBackend(null)} onSaved={agentSaved} orderWrite={{ pending: agentWrites.has(orderAgent.backend), track: (work) => agentWriteRegistry.track(orderAgent.backend, work) }} />}
      <RouteChainDialog selection={routeSelection} sources={sources} onClose={() => setRouteTarget(null)} />
      {adoptAgent && (
        <EnableGatewayDialog
          key={adoptAgent.backend}
          agent={adoptAgent}
          runtime={runtime}
          onClose={() => setAdoptAgent(null)}
          onAdopted={agentSaved}
          onRuntime={(next) => {
            setRuntime(next);
            setRuntimeState(next === null ? 'error' : 'ready');
          }}
          trackWrite={(work) => agentWriteRegistry.track(adoptAgent.backend, work)}
        />
      )}
      {installOpen && runtime && (
        <InstallGatewayDialog
          runtime={runtime}
          onClose={() => setInstallOpen(false)}
          onRuntime={(next) => {
            setRuntime(next);
            setRuntimeState(next === null ? 'error' : 'ready');
            setRuntimeRecoveryPending(next === null);
          }}
        />
      )}
    </ModelHubShell>
  );
};

export default SettingsModelsPage;
