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
import { agentsWithEcho, createLatestAsyncAuthority, createLatestAsyncAuthorityByKey, createPendingWrites, mapWithConcurrency, sourcesWithEcho } from './asyncLifetime';
import { emptyFeed, feedAfterHeadRead, feedAfterTailRead, feedTailCursor, type EventFeed } from './eventFeed';
import { modelsApi, type SourceCreated } from './modelsApi';
import { convergeMutation, createIntentAuthority } from './mutationConvergence';
import { modelChainKey, modelChainRequests, type ModelChainIndex } from './modelRows';
import {
  beginRegionRead,
  failRegionRead,
  loadingRegion,
  readRegion,
  readyRegion,
  regionData,
  regionFailed,
  settleRegionRead,
  unreadRegion,
  type RegionRead,
} from './regionRead';
import { pollRuntimeStatus, runtimeHasInstallAsset, startRuntimeWithStatusRefresh } from './runtimeLifecycle';
import { backendVisual } from './vendorMeta';
import type { AdoptedBy, AgentBackend, AgentSupply, ResolutionEvent, RuntimeDependency, Source } from './types';

const CHAIN_READ_CONCURRENCY = 6;
const EVENT_PAGE = 20;

const readAgentChains = async (agent: AgentSupply): Promise<ModelChainIndex> => Object.fromEntries(
  await mapWithConcurrency(
    modelChainRequests([agent]),
    CHAIN_READ_CONCURRENCY,
    async ({ backend, modelId }) => {
      const key = modelChainKey(backend, modelId);
      try {
        return [key, readyRegion(await modelsApi.getAgentChain(backend, modelId))] as const;
      } catch {
        return [key, unreadRegion()] as const;
      }
    },
  ),
);

const settleAgentChainIndex = (
  previous: ModelChainIndex,
  agent: AgentSupply,
  incoming: ModelChainIndex,
): ModelChainIndex => {
  const prefix = `${agent.backend}\u0000`;
  const next = Object.fromEntries(Object.entries(previous).filter(([key]) => !key.startsWith(prefix)));
  for (const [key, read] of Object.entries(incoming)) {
    next[key] = settleRegionRead(previous[key] ?? loadingRegion(), read);
  }
  return next;
};

const beginAgentChainIndex = (
  previous: ModelChainIndex,
  agent: AgentSupply,
): ModelChainIndex => {
  const prefix = `${agent.backend}\u0000`;
  const next = Object.fromEntries(Object.entries(previous).filter(([key]) => !key.startsWith(prefix)));
  for (const { backend, modelId } of modelChainRequests([agent])) {
    const key = modelChainKey(backend, modelId);
    next[key] = beginRegionRead(previous[key] ?? loadingRegion());
  }
  return next;
};

type SurfaceLanding = {
  sources: RegionRead<Source[]>;
  supply: RegionRead<AgentSupply[]>;
  runtime: RegionRead<RuntimeDependency>;
  events: RegionRead<ResolutionEvent[]>;
};

const readSurfaceLanding = async (): Promise<SurfaceLanding> => {
  const [sources, supply, runtime, events] = await Promise.all([
    readRegion(() => modelsApi.listSources()),
    readRegion(() => modelsApi.listAgents()),
    readRegion(() => modelsApi.getRuntimeStatus()),
    readRegion(() => modelsApi.listEvents(EVENT_PAGE)),
  ]);
  return {
    sources,
    supply,
    runtime,
    events,
  };
};

const surfaceLandingFailed = (landing: SurfaceLanding): boolean =>
  Object.values(landing).some(regionFailed);

export const RuntimePill: React.FC<{
  read: RegionRead<RuntimeDependency>;
  starting: boolean;
  onStart: () => void;
  onInstall: () => void;
}> = ({ read, starting, onStart, onInstall }) => {
  const { t } = useTranslation();
  const runtime = regionData(read);
  if (!runtime) {
    const key = read.kind === 'loading' ? 'starting' : 'unread';
    return <span className={cn('model-hub-runtime-pill', read.kind !== 'loading' && 'model-hub-runtime-pill--error')}><span className="model-hub-runtime-dot" />{read.kind === 'loading' && <LoaderCircle className="animate-spin" />}{t(`settings.models.shell.${key}`)}</span>;
  }
  const health = runtime.status.health;
  const authoritative = read.kind === 'ready';
  const canInstall = health === 'not_installed' && runtimeHasInstallAsset(runtime);
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
  const action = authoritative && !starting && health !== 'installing' && (health === 'down' || health === 'not_started')
    ? onStart
    : authoritative && !starting && canInstall
      ? onInstall
      : null;
  const className = cn(
    'model-hub-runtime-pill',
    (health === 'down' || health === 'degraded' || read.kind === 'error') && 'model-hub-runtime-pill--error',
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
          <div className="model-hub-direct-head flex items-center gap-3 border-b border-border px-5 py-3">
            <div><h2 className="text-[16px] font-bold text-foreground">{t('settings.models.direct.card.current')}</h2><p className="mt-1 text-[11.5px] text-muted">{t('settings.models.direct.card.current.sub')}</p></div>
          </div>
          <div className="model-hub-direct-content flex flex-col">
            {agents.map((agent) => {
              const { Icon } = backendVisual(agent.backend);
              return (
                <div key={agent.backend} className="model-hub-direct-row model-hub-direct-row--backend flex flex-wrap items-center gap-2.5 bg-background px-3 sm:flex-nowrap">
                  <span className="model-hub-direct-tile flex size-[34px] shrink-0 items-center justify-center rounded-[9px]"><Icon className="size-[17px]" /></span>
                  <span className="model-hub-direct-backend-copy">
                    <span className="flex min-w-0 items-center gap-1.5">
                      <span className="model-hub-direct-backend-name truncate text-foreground">{t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend })}</span>
                      <span className="model-hub-direct-kind-pill shrink-0 rounded-full border px-2 py-[3px] text-[10.5px] font-semibold">{t('settings.models.direct.pill.direct')}</span>
                    </span>
                    <span className="model-hub-direct-backend-detail truncate" title={t(`settings.models.direct.backend.${agent.backend}.detail`) as string}>{t(`settings.models.direct.backend.${agent.backend}.detail`)}</span>
                  </span>
                  <Button variant="secondary" size="sm" className="model-hub-direct-switch h-auto rounded-lg px-3 py-[9px] text-[11.5px] font-bold" onClick={() => onSwitch(agent)}>{t('settings.models.direct.action.switchToGateway')}</Button>
                </div>
              );
            })}
          </div>
        </section>
        <section className="model-hub-direct-card overflow-hidden border border-border bg-surface">
          <div className="model-hub-direct-head flex items-center border-b border-border px-5 py-3"><h2 className="text-[16px] font-bold text-foreground">{t('settings.models.direct.benefits.title')}</h2></div>
          <div className="model-hub-direct-content flex flex-col">{(['1', '2', '3'] as const).map((key) => <div key={key} className="model-hub-direct-row flex items-start gap-2.5 bg-background px-3 py-[11px]"><span className="model-hub-ink-mint grid size-5 shrink-0 place-items-center rounded-full bg-mint-soft text-[10.5px] font-bold">{key}</span><span className="model-hub-direct-benefit-copy"><span className="model-hub-direct-benefit-title-row flex items-center gap-1.5"><span className="text-[12.5px] font-semibold text-foreground">{t(`settings.models.direct.benefits.${key}`)}</span></span><span className="model-hub-direct-benefit-detail">{t(`settings.models.direct.benefits.${key}.detail`)}</span></span></div>)}</div>
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
  const [sourcesRead, setSourcesRead] = React.useState<RegionRead<Source[]>>(loadingRegion);
  const [supplyRead, setSupplyRead] = React.useState<RegionRead<AgentSupply[]>>(loadingRegion);
  const [chainsRead, setChainsRead] = React.useState<RegionRead<ModelChainIndex>>(loadingRegion);
  const [runtimeRead, setRuntimeRead] = React.useState<RegionRead<RuntimeDependency>>(loadingRegion);
  const [eventsRead, setEventsRead] = React.useState<RegionRead<EventFeed>>(loadingRegion);
  const [loadingEvents, setLoadingEvents] = React.useState(false);
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
  const [sourceIntentAuthority] = React.useState(createIntentAuthority);
  const overviewRef = React.useRef<HTMLDivElement>(null);
  const aliveRef = React.useRef(true);
  React.useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const sources = regionData(sourcesRead) ?? [];
  const agents = regionData(supplyRead) ?? [];
  const chains = regionData(chainsRead) ?? {};
  const runtime = regionData(runtimeRead) ?? null;
  const feed = regionData(eventsRead) ?? emptyFeed;
  const runtimeHealth = runtime?.status.health ?? null;
  React.useEffect(() => {
    const runtimeCanRecover = runtimeRead.kind === 'unread'
      || runtimeRead.kind === 'error'
      || runtimeRecoveryPending
      || runtimeHealth === 'not_started'
      || runtimeHealth === 'not_installed'
      || runtimeHealth === 'installing'
      || runtimeHealth === 'down'
      || runtimeHealth === 'degraded';
    if (!runtimeCanRecover || startingRuntime) return undefined;
    return pollRuntimeStatus(modelsApi, (nextRuntime) => {
      if (!aliveRef.current) return;
      setRuntimeRead(readyRegion(nextRuntime));
      setRuntimeRecoveryPending(false);
    });
  }, [runtimeHealth, runtimeRead.kind, runtimeRecoveryPending, startingRuntime]);

  const [chainReadAuthority] = React.useState(() => createLatestAsyncAuthorityByKey<AgentBackend, { agent: AgentSupply; chains: ModelChainIndex }>((_backend, incoming) => {
    if (!aliveRef.current) return;
    setChainsRead((previous) => readyRegion(settleAgentChainIndex(regionData(previous) ?? {}, incoming.agent, incoming.chains)));
  }));

  const refreshAgentChains = React.useCallback(async (agent: AgentSupply) => {
    setChainsRead((previous) => readyRegion(beginAgentChainIndex(regionData(previous) ?? {}, agent)));
    await chainReadAuthority.run(agent.backend, async () => ({
      agent,
      chains: await readAgentChains(agent),
    }));
  }, [chainReadAuthority]);

  const refreshAllAgentChains = React.useCallback((agentRows: AgentSupply[]) => {
    const hubAgents = agentRows.filter((agent) => agent.mode === 'hub');
    const activeBackends = new Set(hubAgents.map((agent) => agent.backend));
    setChainsRead((previous) => readyRegion(Object.fromEntries(
      Object.entries(regionData(previous) ?? {}).filter(([key]) => activeBackends.has(key.split('\u0000')[0] as AgentBackend)),
    )));
    for (const agent of hubAgents) void refreshAgentChains(agent);
  }, [refreshAgentChains]);

  const [refreshAuthority] = React.useState(() => createLatestAsyncAuthority<SurfaceLanding>((landing) => {
    if (!aliveRef.current) return;
    setSourcesRead((previous) => settleRegionRead(previous, landing.sources));
    setSupplyRead((previous) => settleRegionRead(previous, landing.supply));
    setRuntimeRead((previous) => {
      const next = settleRegionRead(previous, landing.runtime);
      if (next.kind === 'ready') setRuntimeRecoveryPending(false);
      return next;
    });
    if (landing.supply.kind === 'ready') refreshAllAgentChains(landing.supply.data);
    else setChainsRead(failRegionRead);
    setEventsRead((previous) => {
      if (landing.events.kind !== 'ready') return failRegionRead(previous);
      const previousFeed = regionData(previous);
      return readyRegion(previousFeed
        ? feedAfterHeadRead(previousFeed, landing.events.data)
        : feedAfterTailRead(emptyFeed, landing.events.data, EVENT_PAGE, null));
    });
  }));

  const refresh = React.useCallback(async () => {
    const outcome: { landing: SurfaceLanding | null } = { landing: null };
    const result = await refreshAuthority.run(async () => {
      outcome.landing = await readSurfaceLanding();
      return outcome.landing;
    });
    if (aliveRef.current && result === 'landed' && outcome.landing && surfaceLandingFailed(outcome.landing)) {
      showToast(t('settings.models.toast.refreshFailed') as string, 'error');
    }
  }, [refreshAuthority, showToast, t]);
  React.useEffect(() => {
    void refresh();
  }, [refresh]);

  const retrySources = React.useCallback(async () => {
    setSourcesRead(beginRegionRead);
    await refresh();
  }, [refresh]);

  const retrySupply = React.useCallback(async () => {
    setSupplyRead(beginRegionRead);
    await refresh();
  }, [refresh]);

  const retryEvents = React.useCallback(async () => {
    setEventsRead(beginRegionRead);
    await refresh();
  }, [refresh]);

  const applyAgentEcho = React.useCallback((echoed: AgentSupply) => {
    setSupplyRead((previous) => readyRegion(agentsWithEcho(regionData(previous) ?? [], echoed)));
  }, []);
  const agentSaved = React.useCallback(async (echoed: AgentSupply) => {
    await convergeMutation({
      entity: echoed,
      applyEntity: applyAgentEcho,
      reconcile: refresh,
    });
  }, [applyAgentEcho, refresh]);
  const applySourceEcho = React.useCallback((echoed: Source) => {
    setSourcesRead((previous) => readyRegion(sourcesWithEcho(regionData(previous) ?? [], echoed)));
  }, []);
  const sourceMutation = React.useCallback(async (echoed?: Source) => {
    await convergeMutation({
      entity: echoed,
      applyEntity: applySourceEcho,
      reconcile: refresh,
    });
  }, [applySourceEcho, refresh]);
  const sourceGone = React.useCallback(async (sourceId: string, inventory?: Source[]) => {
    await convergeMutation({
      entity: { sourceId, inventory },
      applyEntity: ({ sourceId: goneId, inventory: authoritative }) => {
        setSourcesRead((previous) => readyRegion(
          authoritative ?? (regionData(previous) ?? []).filter((source) => source.id !== goneId),
        ));
        setAdoptionBySource((previous) => {
          const next = { ...previous };
          delete next[goneId];
          return next;
        });
      },
      reconcile: refresh,
    });
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
      if (aliveRef.current) {
        setEventsRead((previous) => readyRegion(feedAfterTailRead(regionData(previous) ?? emptyFeed, events, EVENT_PAGE, cursor)));
      }
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
      setRuntimeRead((previous) => result.runtime === null
        ? failRegionRead(previous)
        : readyRegion(result.runtime));
      setRuntimeRecoveryPending(result.runtime === null);
      if (result.failed) showToast(t('settings.models.errors.startFailed') as string, 'error');
    } finally {
      setStartingRuntime(false);
    }
  };
  const landingLoading = sourcesRead.kind === 'loading' && regionData(sourcesRead) === undefined
    && supplyRead.kind === 'loading' && regionData(supplyRead) === undefined
    && runtimeRead.kind === 'loading' && regionData(runtimeRead) === undefined
    && eventsRead.kind === 'loading' && regionData(eventsRead) === undefined;
  const directEmpty = regionData(sourcesRead) !== undefined
    && regionData(supplyRead) !== undefined
    && modelsSurfaceKind(agents, sources) === 'direct_empty';
  const selectSource = React.useCallback((sourceId: string | null) => {
    sourceIntentAuthority.commit(() => setSelectedSourceId(sourceId));
  }, [sourceIntentAuthority]);
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
    await convergeMutation({
      entity: created,
      applyEntity: (answer) => {
        setAdoptionBySource((previous) => ({ ...previous, [answer.source.id]: answer.adopted_by }));
        setSourcesRead((previous) => readyRegion([
          answer.source,
          ...(regionData(previous) ?? []).filter((source) => source.id !== answer.source.id),
        ]));
      },
      intent: {
        authority: sourceIntentAuthority,
        apply: () => {
          setApiKeyOpen(false);
          setSelectedSourceId(created.source.id);
        },
      },
      reconcile: refresh,
    });
  };

  return (
    <ModelHubShell
      detailBack={selectedSourceId ? () => selectSource(null) : undefined}
      actions={!landingLoading
        ? directEmpty
          ? <DirectPill count={agents.length} />
          : <span className="flex items-center gap-2">
              <RuntimePill
                read={runtimeRead}
                starting={startingRuntime}
                onStart={() => void startRuntime()}
                onInstall={() => setInstallOpen(true)}
              />
              <TakeoverPill count={takeoverCount} />
            </span>
        : undefined}
    >
      {landingLoading ? <div className="text-[13px] text-muted">{t('common.loading')}</div>
        : selectedSourceId
          ? selectedSource
            ? <SourceDetailPanel source={selectedSource} adoptedBy={adoptionBySource[selectedSource.id]} onMutation={sourceMutation} onGone={sourceGone} />
            : <section className="rounded-xl border border-border bg-surface px-5 py-12 text-center text-[12px] text-muted">{t('settings.models.sourceDetail.gone')}</section>
          : directEmpty ? <DirectHome agents={agents} onSwitch={setAdoptAgent} />
            : <div className="space-y-[22px]">
                  <HubTabs tab={tab} onChange={setTab} />
                  {tab === 'sources' ? <div className="model-hub-overview">
                    <div className="model-hub-overview-body">
                      <div ref={overviewRef} className="model-hub-overview-grid relative flex flex-col gap-4">
                        <SourcesCard read={sourcesRead} adoptionBySource={adoptionBySource} onRetry={() => void retrySources()} onOpenSource={(source) => selectSource(source.id)} onAddApiKey={() => setApiKeyOpen(true)} />
                        <div className="hidden lg:block" aria-hidden="true" />
                        <GatewayModule supply={supplyRead} sources={sources} chains={chains} runtime={runtime} onRetry={() => void retrySupply()} pendingBackends={agentWrites} switchFailures={switchFailures} connectingBackend={adoptAgent?.backend ?? null} onConnectHub={setAdoptAgent} onSwitchDirect={switchToDirect} onOpenOrder={(agent) => setOrderBackend(agent.backend)} onOpenRoute={(agent, modelId) => setRouteTarget({ backend: agent.backend, modelId })} onProbeSettled={(agent) => void refreshAgentChains(agent)} />
                        <SupplyGraph containerRef={overviewRef} relations={supplyRelations} />
                      </div>
                      <SupplyLegend relations={supplyRelations} />
                    </div>
                    <RecentSwitchesCard events={eventsRead} sources={sourcesRead} onRetry={retryEvents} loadingMore={loadingEvents} onLoadMore={loadOlderEvents} />
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
            setRuntimeRead((previous) => next === null ? failRegionRead(previous) : readyRegion(next));
          }}
          trackWrite={(work) => agentWriteRegistry.track(adoptAgent.backend, work)}
        />
      )}
      {installOpen && runtime && (
        <InstallGatewayDialog
          runtime={runtime}
          onClose={() => setInstallOpen(false)}
          onRuntime={(next) => {
            setRuntimeRead((previous) => next === null ? failRegionRead(previous) : readyRegion(next));
            setRuntimeRecoveryPending(next === null);
          }}
        />
      )}
    </ModelHubShell>
  );
};

export default SettingsModelsPage;
