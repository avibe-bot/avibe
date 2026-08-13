import * as React from 'react';
import { ArrowLeft, Gauge, Info, LoaderCircle, Route, Sparkles } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { useToast } from '@/context/ToastContext';
import { cn } from '@/lib/utils';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import { OAuthConnectDialog } from './OAuthConnectDialog';
import { AdvancedRow } from './AdvancedRow';
import { EnableGatewayDialog } from './EnableGatewayDialog';
import { GatewayModule } from './GatewayModule';
import { InstallGatewayDialog } from './InstallGatewayDialog';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import { RecentSwitchesCard } from './RecentSwitchesCard';
import { RouteChainDialog, type RouteCollectionObservation, type RouteCommitReconciliation, type RouteReport } from './RouteChainDialog';
import { routeChainMatchesAttempt } from './routeChainDraft';
import { SourceDetailPanel } from './SourceDetailPanel';
import { SourceOrderDrawer } from './SourceOrderDrawer';
import { SourcesCard } from './SourcesCard';
import { modelsSurfaceKindFromReads } from './modelHubSurfaceState';
import { focusModelHubProjection } from './modelHubFocus';
import { buildSupplyRelations } from './supplyRelations';
import {
  emptySuspendedRouteAttempts,
  holdSuspendedRouteAttempt,
  releaseSuspendedRouteAttempt,
} from './suspendedRouteAttempts';
import { SupplyGraph, SupplyLegend } from './SupplyGraph';
import './modelHubSurface.css';
import { agentsWithEcho, createLatestAsyncAuthority, createLatestAsyncAuthorityByKey, createLatestEntityAuthorityByKey, createPendingWrites, mapWithConcurrency } from './asyncLifetime';
import { createAgentCollectionReadAuthority, createSourceCollectionReadAuthority, type CollectionReadAuthority } from './collectionReadAuthority';
import { emptyFeed, feedAfterHeadRead, feedAfterTailRead, feedTailCursor, type EventFeed } from './eventFeed';
import { readFirstPaintRegions, type SurfaceLanding } from './firstPaintRegions';
import { modelsApi, type SourceCreated } from './modelsApi';
import { convergeMutation, createIntentAuthority } from './mutationConvergence';
import type { SourceMutationSettlement, TrackSourceMutation } from './mutationSettlement';
import { modelChainKey, modelChainRequests, type ModelChainIndex } from './modelRows';
import {
  beginRegionRead,
  failRegionRead,
  foldRegionRead,
  degradedRegion,
  loadingRegion,
  readyRegion,
  regionFailed,
  settleRegionRead,
  unreadRegion,
  type RegionRead,
} from './regionRead';
import { freshRuntimeProjection, pollRuntimeStatus, runtimeHasInstallAsset, startRuntimeWithStatusRefresh } from './runtimeLifecycle';
import { createRouteProjectionReconciler, type RouteProjectionStatus } from './routeProjectionReconciliation';
import { backendVisual } from './vendorMeta';
import type { AgentBackend, AgentSupply, ResolutionEvent, RuntimeDependency, Source } from './types';

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

const readExactAgentChain = async (
  agent: AgentSupply,
  modelId: string,
): Promise<ModelChainIndex> => ({
  [modelChainKey(agent.backend, modelId)]: readyRegion(
    await modelsApi.getAgentChain(agent.backend, modelId),
  ),
});

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

const settleExactAgentChain = (
  previous: ModelChainIndex,
  incoming: ModelChainIndex,
): ModelChainIndex => {
  const next = { ...previous };
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

type AuthorizedSurfaceLanding = {
  landing: SurfaceLanding;
  sourceSnapshot: number;
};

const readSurfaceLanding = (
  sourceReads: CollectionReadAuthority<Source[]>,
  agentReads: CollectionReadAuthority<AgentSupply[]>,
): Promise<SurfaceLanding> => readFirstPaintRegions({
  sources: sourceReads.readValue,
  supply: agentReads.readValue,
  runtime: () => modelsApi.getRuntimeStatus(),
});

const surfaceLandingFailed = (landing: SurfaceLanding): boolean =>
  Object.values(landing).some(regionFailed);

export const RuntimePill: React.FC<{
  read: RegionRead<RuntimeDependency>;
  starting: boolean;
  onStart: () => void;
  onInstall: () => void;
  directCount?: number;
}> = ({ read, starting, onStart, onInstall, directCount }) => {
  const { t } = useTranslation();
  const projection = foldRegionRead<RuntimeDependency, { runtime: RuntimeDependency; authoritative: boolean } | null>(read, {
    loading: () => null,
    ready: (runtime) => ({ runtime, authoritative: true }),
    unread: () => null,
    degraded: (runtime) => ({ runtime, authoritative: false }),
  });
  if (!projection) {
    const key = read.kind === 'loading' ? 'starting' : 'unread';
    return <span className={cn('model-hub-runtime-pill', read.kind !== 'loading' && 'model-hub-runtime-pill--error')}><span className="model-hub-runtime-dot" />{read.kind === 'loading' && <LoaderCircle className="animate-spin" />}{t(`settings.models.shell.${key}`)}</span>;
  }
  const { runtime, authoritative } = projection;
  const health = runtime.status.health;
  const unread = read.kind === 'degraded' && read.cause === 'read_failed';
  const canInstall = health === 'not_installed' && runtimeHasInstallAsset(runtime);
  const allDirect = authoritative && !starting && health === 'ok' && directCount !== undefined && directCount > 0;
  const key = unread
    ? 'unread'
    : starting
    ? 'starting'
    : health === 'installing'
      ? 'starting'
    : health === 'ok'
      ? allDirect ? 'allDirect' : 'running'
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
    (health === 'down' || health === 'degraded' || unread) && 'model-hub-runtime-pill--error',
    allDirect && 'model-hub-runtime-pill--direct',
  );
  const content = <><span className="model-hub-runtime-dot" />{(starting || health === 'installing') && <LoaderCircle className="animate-spin" />}{t(`settings.models.shell.${key}`, allDirect ? { count: directCount } : undefined)}</>;
  return action
    ? <button type="button" className={className} onClick={action}>{content}</button>
    : <span className={className}>{content}</span>;
};

const ModelHubShell: React.FC<{ actions?: React.ReactNode; detailBack?: () => void; children: React.ReactNode; rootRef?: React.Ref<HTMLDivElement> }> = ({ actions, detailBack, children, rootRef }) => {
  const { t } = useTranslation();
  return (
    <div ref={rootRef} className="model-hub-shell">
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
  const [subscriptionPickerOpen, setSubscriptionPickerOpen] = React.useState(false);
  const [subscriptionVendor, setSubscriptionVendor] = React.useState<string | null>(null);
  const subscriptionTriggerRef = React.useRef<HTMLButtonElement>(null);
  const subscriptionCloseTimer = React.useRef<number | null>(null);
  const sourceDetailHeadingRef = React.useRef<HTMLHeadingElement>(null);
  const focusSourceDetailPendingRef = React.useRef(false);
  const [orderBackend, setOrderBackend] = React.useState<AgentBackend | null>(null);
  const [adoptAgent, setAdoptAgent] = React.useState<AgentSupply | null>(null);
  const [routeTarget, setRouteTarget] = React.useState<{ agent: AgentSupply; modelId: string; opener: HTMLElement | null } | null>(null);
  const [routeCommitStatus, setRouteCommitStatus] = React.useState<RouteProjectionStatus | null>(null);
  const [routeCommitBackend, setRouteCommitBackend] = React.useState<AgentBackend | null>(null);
  const [suspendedRouteAttempts, setSuspendedRouteAttempts] = React.useState(
    emptySuspendedRouteAttempts,
  );
  const suspendedHubFrontiersRef = React.useRef(new Map<AgentBackend, AgentSupply>());
  const suspendedSourceBaselinesRef = React.useRef(
    new Map<AgentBackend, RegionRead<Source[]>>(),
  );
  const suspendedChainBaselinesRef = React.useRef(
    new Map<AgentBackend, RegionRead<ModelChainIndex>>(),
  );
  const [selectedSourceId, setSelectedSourceId] = React.useState<string | null>(null);
  const [agentWrites, setAgentWrites] = React.useState<ReadonlySet<string>>(() => new Set());
  const [switchFailures, setSwitchFailures] = React.useState<ReadonlySet<string>>(() => new Set());
  const [agentWriteRegistry] = React.useState(() => createPendingWrites(setAgentWrites));
  const [sourceIntentAuthority] = React.useState(createIntentAuthority);
  const [sourceCollectionReads] = React.useState(() => createSourceCollectionReadAuthority(modelsApi));
  const [agentCollectionReads] = React.useState(() => createAgentCollectionReadAuthority(modelsApi));
  const overviewRef = React.useRef<HTMLDivElement>(null);
  const pageRef = React.useRef<HTMLDivElement>(null);
  const aliveRef = React.useRef(true);
  const [sourceEntityAuthority] = React.useState(() => createLatestEntityAuthorityByKey(
    (source: Source) => source.id,
    (inventory) => {
      if (aliveRef.current) setSourcesRead(readyRegion(inventory));
    },
  ));
  const [sourceWriteRegistry] = React.useState(() => createPendingWrites(() => {}));
  React.useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const sources = foldRegionRead<Source[], Source[]>(sourcesRead, {
    loading: () => [],
    ready: (data) => data,
    unread: () => [],
    degraded: (staleData) => staleData,
  });
  const agents = foldRegionRead<AgentSupply[], AgentSupply[]>(supplyRead, {
    loading: () => [],
    ready: (data) => data,
    unread: () => [],
    degraded: (staleData) => staleData,
  });
  const chains = foldRegionRead<ModelChainIndex, ModelChainIndex>(chainsRead, {
    loading: () => ({}),
    ready: (data) => data,
    unread: () => ({}),
    degraded: () => ({}),
  });
  const runtime = freshRuntimeProjection(runtimeRead);
  const retainedRuntime = foldRegionRead<RuntimeDependency, RuntimeDependency | null>(runtimeRead, {
    loading: () => null,
    ready: (data) => data,
    unread: () => null,
    degraded: (staleData) => staleData,
  });
  const feed = foldRegionRead<EventFeed, EventFeed>(eventsRead, {
    loading: () => emptyFeed,
    ready: (data) => data,
    unread: () => emptyFeed,
    degraded: (staleData) => staleData,
  });
  const runtimeHealth = retainedRuntime?.status.health ?? null;
  React.useEffect(() => {
    const runtimeCanRecover = runtimeRead.kind === 'unread'
      || (runtimeRead.kind === 'degraded' && runtimeRead.cause === 'read_failed')
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

  const [chainReadAuthority] = React.useState(() => createLatestAsyncAuthorityByKey<AgentBackend, { agent: AgentSupply; chains: ModelChainIndex; scope: 'backend' | 'model' }>((_backend, incoming) => {
    if (!aliveRef.current) return;
    setChainsRead((previous) => {
      const current = foldRegionRead<ModelChainIndex, ModelChainIndex>(previous, {
        loading: () => ({}),
        ready: (data) => data,
        unread: () => ({}),
        degraded: (staleData) => staleData,
      });
      return readyRegion(incoming.scope === 'backend'
        ? settleAgentChainIndex(current, incoming.agent, incoming.chains)
        : settleExactAgentChain(current, incoming.chains));
    });
  }));

  const refreshAgentChains = React.useCallback(async (agent: AgentSupply) => {
    setChainsRead((previous) => readyRegion(beginAgentChainIndex(foldRegionRead<ModelChainIndex, ModelChainIndex>(previous, {
      loading: () => ({}),
      ready: (data) => data,
      unread: () => ({}),
      degraded: (staleData) => staleData,
    }), agent)));
    await chainReadAuthority.run(agent.backend, async () => ({
      agent,
      chains: await readAgentChains(agent),
      scope: 'backend' as const,
    }));
  }, [chainReadAuthority]);

  const refreshAllAgentChains = React.useCallback((agentRows: AgentSupply[]) => {
    const hubAgents = agentRows.filter((agent) => agent.mode === 'hub');
    const suspendedBackends = new Set(suspendedRouteAttempts.keys());
    const probeAgents = hubAgents.filter((agent) =>
      !suspendedBackends.has(agent.backend)
      && agent.backend !== routeCommitBackend);
    const activeBackends = new Set(hubAgents.map((agent) => agent.backend));
    chainReadAuthority.invalidateExcept(activeBackends);
    setChainsRead((previous) => readyRegion(Object.fromEntries(
      Object.entries(foldRegionRead<ModelChainIndex, ModelChainIndex>(previous, {
        loading: () => ({}),
        ready: (data) => data,
        unread: () => ({}),
        degraded: (staleData) => staleData,
      })).filter(([key]) => activeBackends.has(key.split('\u0000')[0] as AgentBackend)),
    )));
    for (const agent of probeAgents) void refreshAgentChains(agent);
  }, [chainReadAuthority, refreshAgentChains, routeCommitBackend, suspendedRouteAttempts]);

  React.useEffect(() => {
    const freshSupply = foldRegionRead<AgentSupply[], AgentSupply[] | null>(supplyRead, {
      loading: () => null,
      ready: (data) => data,
      unread: () => null,
      degraded: () => null,
    });
    if (freshSupply) {
      refreshAllAgentChains(freshSupply);
      setSwitchFailures((previous) => new Set(
        [...previous].filter((backend) => freshSupply.some((agent) => agent.backend === backend && agent.mode === 'hub')),
      ));
    }
  }, [refreshAllAgentChains, supplyRead]);

  const [eventReadAuthority] = React.useState(() => createLatestAsyncAuthority<RegionRead<ResolutionEvent[]>>((incoming) => {
    if (!aliveRef.current) return;
    setEventsRead((previous) => {
      if (incoming.kind !== 'ready') return failRegionRead(previous);
      const previousFeed = foldRegionRead<EventFeed, EventFeed | undefined>(previous, {
        loading: () => undefined,
        ready: (data) => data,
        unread: () => undefined,
        degraded: (staleData) => staleData,
      });
      const freshEvents = foldRegionRead<ResolutionEvent[], ResolutionEvent[] | null>(incoming, {
        loading: () => null,
        ready: (data) => data,
        unread: () => null,
        degraded: () => null,
      });
      if (!freshEvents) return failRegionRead(previous);
      return readyRegion(previousFeed
        ? feedAfterHeadRead(previousFeed, freshEvents)
        : feedAfterTailRead(emptyFeed, freshEvents, EVENT_PAGE, null));
    });
  }));

  const refreshEventHead = React.useCallback(async () => {
    setEventsRead(beginRegionRead);
    await eventReadAuthority.run(async () => {
      try {
        return readyRegion(await modelsApi.listEvents(EVENT_PAGE));
      } catch {
        return unreadRegion<ResolutionEvent[]>();
      }
    });
  }, [eventReadAuthority]);

  const [refreshAuthority] = React.useState(() => createLatestAsyncAuthority<AuthorizedSurfaceLanding>(({ landing, sourceSnapshot }) => {
    if (!aliveRef.current) return;
    const freshSources = foldRegionRead<Source[], Source[] | null>(landing.sources, {
      loading: () => null,
      ready: (data) => data,
      unread: () => null,
      degraded: () => null,
    });
    if (freshSources) sourceEntityAuthority.settleSnapshot(sourceSnapshot, freshSources);
    else setSourcesRead((previous) => settleRegionRead(previous, landing.sources));
    setSupplyRead((previous) => settleRegionRead(previous, landing.supply));
    setRuntimeRead((previous) => {
      const next = settleRegionRead(previous, landing.runtime);
      if (next.kind === 'ready') setRuntimeRecoveryPending(false);
      return next;
    });
    if (landing.supply.kind !== 'ready') setChainsRead(failRegionRead);
  }));

  const refresh = React.useCallback(async () => {
    void refreshEventHead();
    const outcome: { landing: SurfaceLanding | null } = { landing: null };
    const result = await refreshAuthority.run(async () => {
      const sourceSnapshot = sourceEntityAuthority.beginSnapshot();
      outcome.landing = await readSurfaceLanding(sourceCollectionReads, agentCollectionReads);
      return { landing: outcome.landing, sourceSnapshot };
    });
    if (aliveRef.current && result === 'landed' && outcome.landing && surfaceLandingFailed(outcome.landing)) {
      showToast(t('settings.models.toast.refreshFailed') as string, 'error');
    }
  }, [agentCollectionReads, refreshAuthority, refreshEventHead, showToast, sourceCollectionReads, sourceEntityAuthority, t]);

  const trackSourceMutation = React.useCallback((sourceId: string): TrackSourceMutation => async <T,>(work: (source: Source, settlement: SourceMutationSettlement) => Promise<T>): Promise<T> => {
    let result!: T;
    await sourceWriteRegistry.track(sourceId, async () => {
      const current = sourceEntityAuthority.current(sourceId);
      if (!current) throw new Error(`Source ${sourceId} is no longer available`);
      const generation = sourceEntityAuthority.begin(sourceId);
      let settled = false;
      const finish = async (apply: () => void, reconcile = true): Promise<void> => {
        if (settled) return;
        settled = true;
        apply();
        if (reconcile) await refresh();
      };
      const settlement: SourceMutationSettlement = {
        source: async (echoed) => finish(() => { sourceEntityAuthority.settle(generation, echoed); }),
        gone: async (goneId, inventory) => finish(() => {
          if (inventory) {
            sourceEntityAuthority.settleSnapshotEntries(
              inventory.snapshot,
              inventory.sources.filter((source) => source.id !== goneId),
            );
          }
          if (goneId === sourceId) sourceEntityAuthority.settleRemoval(generation);
        }),
        unread: async () => finish(() => { sourceEntityAuthority.abandon(generation); }),
        release: () => { void finish(() => { sourceEntityAuthority.abandon(generation); }, false); },
        readInventory: async () => {
          const snapshot = sourceEntityAuthority.beginSnapshot();
          return { snapshot, sources: await sourceCollectionReads.readValue() };
        },
      };
      try {
        result = await work(current, settlement);
      } finally {
        settlement.release();
      }
    });
    return result;
  }, [refresh, sourceCollectionReads, sourceEntityAuthority, sourceWriteRegistry]);

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
    await refreshEventHead();
  }, [refreshEventHead]);

  const applyAgentEcho = React.useCallback((echoed: AgentSupply) => {
    setSupplyRead((previous) => readyRegion(agentsWithEcho(foldRegionRead(previous, {
      loading: () => [],
      ready: (data) => data,
      unread: () => [],
      degraded: (staleData) => staleData,
    }), echoed)));
  }, []);
  const agentSaved = React.useCallback(async (echoed: AgentSupply) => {
    await convergeMutation({
      entity: echoed,
      applyEntity: applyAgentEcho,
      reconcile: refresh,
    });
  }, [applyAgentEcho, refresh]);
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
        try {
          const result = await agentCollectionReads.read();
          if (result.kind === 'stale') return;
          const authoritative = result.value;
          setSupplyRead(readyRegion(authoritative));
          const committed = authoritative.some((row) => row.backend === agent.backend && row.mode === 'direct');
          setSwitchFailures((previous) => {
            const next = new Set(previous);
            if (committed) next.delete(agent.backend);
            else next.add(agent.backend);
            return next;
          });
        } catch {
          setSwitchFailures((previous) => new Set(previous).add(agent.backend));
        }
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
        setEventsRead((previous) => readyRegion(feedAfterTailRead(foldRegionRead(previous, {
          loading: () => emptyFeed,
          ready: (data) => data,
          unread: () => emptyFeed,
          degraded: (staleData) => staleData,
        }), events, EVENT_PAGE, cursor)));
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
  const landingLoading = sourcesRead.kind === 'loading'
    && supplyRead.kind === 'loading'
    && runtimeRead.kind === 'loading';
  const directEmpty = modelsSurfaceKindFromReads(supplyRead, sourcesRead) === 'direct_empty';
  const installedAgents = agents.filter((agent) => agent.cli_present);
  const installedSupplyRead = foldRegionRead<AgentSupply[], RegionRead<AgentSupply[]>>(supplyRead, {
    loading: () => loadingRegion(),
    ready: () => readyRegion(installedAgents),
    unread: (retryable) => unreadRegion(retryable),
    degraded: (_staleData, cause, retryable) => degradedRegion(installedAgents, cause, retryable),
  });
  const selectSource = React.useCallback((sourceId: string | null) => {
    sourceIntentAuthority.commit(() => setSelectedSourceId(sourceId));
  }, [sourceIntentAuthority]);
  const selectedSource = sources.find((source) => source.id === selectedSourceId) ?? null;
  const orderAgent = agents.find((agent) => agent.backend === orderBackend && agent.mode === 'hub') ?? null;
  const currentRouteAgent = routeTarget
    ? installedAgents.find((agent) => agent.backend === routeTarget.agent.backend) ?? null
    : null;
  const routeSelection = routeTarget ? {
    agent: currentRouteAgent ?? routeTarget.agent,
    modelId: routeTarget.modelId,
    read: chains[modelChainKey(routeTarget.agent.backend, routeTarget.modelId)],
    available: supplyRead.kind !== 'ready' || currentRouteAgent !== null,
  } : null;
  const focusRouteDestination = React.useCallback((target: NonNullable<typeof routeTarget>) => {
    requestAnimationFrame(() => {
      focusModelHubProjection({
        root: pageRef.current,
        activeTarget: target.opener,
        backend: target.agent.backend,
        modelId: target.modelId,
      });
    });
  }, []);
  const routeObserved = React.useCallback((next: RouteReport['chain']) => {
    setChainsRead((previous) => readyRegion({
      ...foldRegionRead<ModelChainIndex, ModelChainIndex>(previous, {
        loading: () => ({}),
        ready: (data) => data,
        unread: () => ({}),
        degraded: (staleData) => staleData,
      }),
      [modelChainKey(next.backend, next.model_id)]: readyRegion(next),
    }));
  }, []);
  const readRouteAgents = React.useCallback(async (): Promise<RouteCollectionObservation<AgentSupply[]>> => {
    const result = await agentCollectionReads.read();
    if (result.kind === 'stale') throw new Error('route_agents_read_superseded');
    return {
      value: result.value,
      install: () => setSupplyRead(readyRegion(result.value)),
    };
  }, [agentCollectionReads]);
  const readRouteSources = React.useCallback(async (): Promise<RouteCollectionObservation<Source[]>> => {
    const snapshot = sourceEntityAuthority.beginSnapshot();
    const result = await sourceCollectionReads.read();
    if (result.kind === 'stale') throw new Error('route_sources_read_superseded');
    return {
      value: result.value,
      install: () => sourceEntityAuthority.settleSnapshot(snapshot, result.value),
    };
  }, [sourceCollectionReads, sourceEntityAuthority]);
  const routeProjectionReconciler = React.useMemo(() => createRouteProjectionReconciler({
    readAgents: readRouteAgents,
    readSources: readRouteSources,
    onFailure: (member) => {
      if (member === 'agents') setSupplyRead(failRegionRead);
      else setSourcesRead(failRegionRead);
    },
    onStatus: setRouteCommitStatus,
  }), [readRouteAgents, readRouteSources]);
  const routeCommitted = React.useCallback((result: RouteReport) => {
    chainReadAuthority.invalidate(result.chain.backend);
    routeObserved(result.chain);
    setSuspendedRouteAttempts((attempts) =>
      releaseSuspendedRouteAttempt(attempts, result.chain.backend),
    );
    suspendedHubFrontiersRef.current.delete(result.chain.backend);
    suspendedSourceBaselinesRef.current.delete(result.chain.backend);
    suspendedChainBaselinesRef.current.delete(result.chain.backend);
    setRouteCommitBackend(result.chain.backend);
    routeProjectionReconciler.start(result.chain.backend);
  }, [chainReadAuthority, routeObserved, routeProjectionReconciler]);
  React.useEffect(() => {
    if (routeCommitStatus && !routeCommitStatus.pending && routeCommitStatus.failed.size === 0) {
      setRouteCommitBackend(null);
    }
  }, [routeCommitStatus]);
  const retryRouteCommit = React.useCallback(() => {
    if (routeCommitStatus?.pending || !routeCommitStatus?.failed.size) return;
    routeProjectionReconciler.retry();
  }, [routeCommitStatus, routeProjectionReconciler]);
  const routeCommitReconciliation = React.useMemo<RouteCommitReconciliation | null>(() => routeCommitStatus ? ({
    pending: routeCommitStatus.pending,
    failed: routeCommitStatus.failed.size > 0,
    retry: retryRouteCommit,
  }) : null, [retryRouteCommit, routeCommitStatus]);
  React.useEffect(() => {
    const heldBackends = new Set(suspendedRouteAttempts.keys());
    for (const backend of suspendedHubFrontiersRef.current.keys()) {
      if (!heldBackends.has(backend)) suspendedHubFrontiersRef.current.delete(backend);
    }
    for (const backend of suspendedSourceBaselinesRef.current.keys()) {
      if (!heldBackends.has(backend)) suspendedSourceBaselinesRef.current.delete(backend);
    }
    for (const backend of suspendedChainBaselinesRef.current.keys()) {
      if (!heldBackends.has(backend)) suspendedChainBaselinesRef.current.delete(backend);
    }
    const freshAgents = foldRegionRead<AgentSupply[], AgentSupply[] | null>(supplyRead, {
      loading: () => null,
      ready: (data) => data,
      unread: () => null,
      degraded: () => null,
    });
    for (const held of suspendedRouteAttempts.values()) {
      const freshAgent = freshAgents?.find((row) => row.backend === held.backend) ?? null;
      if (!freshAgent || freshAgent.mode !== 'hub') {
        suspendedHubFrontiersRef.current.delete(held.backend);
        suspendedSourceBaselinesRef.current.set(held.backend, sourcesRead);
        continue;
      }
      if (sourcesRead === suspendedSourceBaselinesRef.current.get(held.backend) || sourcesRead.kind !== 'ready') continue;
      if (suspendedHubFrontiersRef.current.get(held.backend) === freshAgent) continue;
      suspendedHubFrontiersRef.current.set(held.backend, freshAgent);
      suspendedChainBaselinesRef.current.set(held.backend, chainsRead);
      void (async () => {
        try {
          await chainReadAuthority.run(held.backend, async () => ({
            agent: freshAgent,
            chains: await readExactAgentChain(freshAgent, held.modelId),
            scope: 'model' as const,
          }));
          if (suspendedHubFrontiersRef.current.get(held.backend) !== freshAgent) return;
        } catch {
          if (suspendedHubFrontiersRef.current.get(held.backend) !== freshAgent) return;
          setChainsRead((previous) => readyRegion({
            ...foldRegionRead<ModelChainIndex, ModelChainIndex>(previous, {
              loading: () => ({}),
              ready: (data) => data,
              unread: () => ({}),
              degraded: (staleData) => staleData,
            }),
            [modelChainKey(held.backend, held.modelId)]: unreadRegion(),
          }));
        }
      })();
    }
  }, [chainReadAuthority, chainsRead, sourcesRead, supplyRead, suspendedRouteAttempts]);
  React.useEffect(() => {
    for (const held of suspendedRouteAttempts.values()) {
      if (!suspendedHubFrontiersRef.current.has(held.backend) || chainsRead === suspendedChainBaselinesRef.current.get(held.backend)) continue;
      const observed = foldRegionRead(chainsRead, {
        loading: () => null,
        ready: (index) => foldRegionRead(index[modelChainKey(held.backend, held.modelId)] ?? unreadRegion(), {
          loading: () => null,
          ready: (data) => data,
          unread: () => null,
          degraded: () => null,
        }),
        unread: () => null,
        degraded: () => null,
      });
      if (observed && routeChainMatchesAttempt(observed, held)) {
        routeCommitted({ chain: observed, removed_hops: null, interrupted: null });
      }
    }
  }, [chainsRead, routeCommitted, suspendedRouteAttempts]);
  const supplyRelations = React.useMemo(() => buildSupplyRelations(installedAgents, sources, chains, runtime), [chains, installedAgents, runtime, sources]);
  const takeoverCount = React.useMemo(() => new Set(
    supplyRelations.filter(({ kind }) => kind === 'takeover').map(({ backend }) => backend),
  ).size, [supplyRelations]);
  const sourceAdded = async (created: SourceCreated) => {
    await convergeMutation({
      entity: created,
      applyEntity: (answer) => {
        sourceEntityAuthority.landLatest(answer.source);
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
  const subscriptionAdded = React.useCallback((source?: Source) => {
    if (!source) {
      void refresh();
      return;
    }
    // Keep the success panel readable until its existing handoff timer closes it;
    // the effect below then moves focus into the committed source detail surface.
    focusSourceDetailPendingRef.current = true;
    sourceEntityAuthority.landLatest(source);
    selectSource(source.id);
    void refresh();
    if (subscriptionCloseTimer.current !== null) window.clearTimeout(subscriptionCloseTimer.current);
    subscriptionCloseTimer.current = window.setTimeout(() => {
      subscriptionCloseTimer.current = null;
      setSubscriptionVendor(null);
    }, 1400);
  }, [refresh, selectSource, sourceEntityAuthority]);
  React.useEffect(() => {
    if (!focusSourceDetailPendingRef.current || subscriptionVendor !== null || !selectedSourceId || !selectedSource) return;
    focusSourceDetailPendingRef.current = false;
    window.requestAnimationFrame(() => sourceDetailHeadingRef.current?.focus());
  }, [selectedSource, selectedSourceId, subscriptionVendor]);
  const closeSubscription = React.useCallback(() => {
    if (subscriptionCloseTimer.current !== null) {
      window.clearTimeout(subscriptionCloseTimer.current);
      subscriptionCloseTimer.current = null;
    }
    setSubscriptionVendor(null);
    window.setTimeout(() => subscriptionTriggerRef.current?.focus(), 0);
  }, []);
  React.useEffect(() => () => {
    if (subscriptionCloseTimer.current !== null) window.clearTimeout(subscriptionCloseTimer.current);
  }, []);

  return (
    <ModelHubShell
      rootRef={pageRef}
      detailBack={selectedSourceId ? () => selectSource(null) : undefined}
      actions={!landingLoading
        ? directEmpty && installedAgents.length === 0
          ? undefined
          : <span className="flex items-center gap-2">
              <RuntimePill
                read={runtimeRead}
                starting={startingRuntime}
                onStart={() => void startRuntime()}
                onInstall={() => setInstallOpen(true)}
                directCount={directEmpty ? installedAgents.length : undefined}
              />
              {!directEmpty && <TakeoverPill count={takeoverCount} />}
            </span>
        : undefined}
    >
      {landingLoading ? <div className="text-[13px] text-muted">{t('common.loading')}</div>
        : selectedSourceId
          ? selectedSource
            ? <SourceDetailPanel source={selectedSource} headingRef={sourceDetailHeadingRef} trackMutation={trackSourceMutation(selectedSource.id)} />
            : <section className="rounded-xl border border-border bg-surface px-5 py-12 text-center text-[12px] text-muted">{t('settings.models.sourceDetail.gone')}</section>
          : directEmpty ? <DirectHome agents={installedAgents} onSwitch={setAdoptAgent} />
            : <div className="space-y-[22px]">
                  <HubTabs tab={tab} onChange={setTab} />
                  {tab === 'sources' ? <div className="model-hub-overview">
                    <div className="model-hub-overview-body">
                      <div ref={overviewRef} className="model-hub-overview-grid relative flex flex-col gap-4">
                        <SourcesCard read={sourcesRead} readFailureCopy={routeCommitStatus?.failed.has('sources') ? t('settings.models.routeDialog.impact.refreshFail') : undefined} onRetry={() => routeCommitStatus?.failed.has('sources') ? retryRouteCommit() : void retrySources()} onOpenSource={(source) => selectSource(source.id)} onAddApiKey={() => setApiKeyOpen(true)} onAddSubscription={() => setSubscriptionPickerOpen(true)} subscriptionTriggerRef={subscriptionTriggerRef} />
                        <div className="hidden lg:block" aria-hidden="true" />
                        <GatewayModule supply={installedSupplyRead} readFailureCopy={routeCommitStatus?.failed.has('agents') ? t('settings.models.routeDialog.impact.refreshFail') : undefined} sources={sources} chains={chains} runtime={runtime} runtimeSnapshot={retainedRuntime} onRetry={() => routeCommitStatus?.failed.has('agents') ? retryRouteCommit() : void retrySupply()} pendingBackends={agentWrites} switchFailures={switchFailures} connectingBackend={adoptAgent?.backend ?? null} onConnectHub={setAdoptAgent} onSwitchDirect={switchToDirect} onOpenOrder={(agent) => setOrderBackend(agent.backend)} onOpenRoute={(agent, modelId, opener) => setRouteTarget({ agent, modelId, opener })} onProbeSettled={(agent) => void refreshAgentChains(agent)} />
                        <SupplyGraph containerRef={overviewRef} relations={supplyRelations} />
                      </div>
                      <SupplyLegend relations={supplyRelations} />
                    </div>
                    <RecentSwitchesCard events={eventsRead} sources={sourcesRead} onRetry={retryEvents} loadingMore={loadingEvents} onLoadMore={loadOlderEvents} />
                    <AdvancedRow />
                  </div> : <section className="rounded-xl border border-border bg-surface px-5 py-8"><div className="flex items-start gap-3"><Info className="mt-0.5 size-4 text-muted" /><div><h2 className="text-[14px] font-semibold text-foreground">{t('settings.models.usageTab.title')}</h2><p className="mt-1 text-[12px] text-muted">{t('settings.models.usageTab.detail')}</p></div></div></section>}
                </div>}
      <AddApiKeyDialog open={apiKeyOpen} sourceReads={sourceCollectionReads} onClose={() => setApiKeyOpen(false)} onAdded={(created) => void sourceAdded(created)} />
      <Dialog open={subscriptionPickerOpen} onOpenChange={setSubscriptionPickerOpen}>
        <DialogContent className="max-w-[420px]">
          <DialogHeader>
            <DialogTitle>{t('settings.models.subscriptionPicker.title')}</DialogTitle>
            <DialogDescription>{t('settings.models.subscriptionPicker.detail')}</DialogDescription>
          </DialogHeader>
          <div className="grid gap-2 sm:grid-cols-2">
            {(['anthropic', 'openai'] as const).map((vendor) => (
              <Button
                key={vendor}
                type="button"
                variant="outline"
                className="h-auto justify-start gap-3 px-3 py-3 text-left"
                onClick={() => {
                  setSubscriptionPickerOpen(false);
                  setSubscriptionVendor(vendor);
                }}
              >
                <span className="model-hub-accent-tile--mint grid size-8 shrink-0 place-items-center rounded-lg"><Sparkles className="model-hub-accent-ink--mint size-4" /></span>
                <span className="flex flex-col items-start">
                  <span className="font-semibold">{t(`settings.models.subscriptionPicker.${vendor}.title`)}</span>
                  <span className="text-[11px] text-muted">{t(`settings.models.subscriptionPicker.${vendor}.detail`)}</span>
                </span>
              </Button>
            ))}
          </div>
        </DialogContent>
      </Dialog>
      {subscriptionVendor && (
        <OAuthConnectDialog
          open
          vendor={subscriptionVendor}
          sources={sources}
          onClose={closeSubscription}
          onConnected={subscriptionAdded}
        />
      )}
      {orderAgent && <SourceOrderDrawer open agent={orderAgent} sources={sources} sourceReads={sourceCollectionReads} onClose={() => setOrderBackend(null)} onSaved={agentSaved} orderWrite={{ pending: agentWrites.has(orderAgent.backend), track: (work) => agentWriteRegistry.track(orderAgent.backend, work) }} />}
      <RouteChainDialog
        selection={routeSelection}
        sources={sources}
        onClose={() => {
          const target = routeTarget;
          setRouteTarget(null);
          if (target) focusRouteDestination(target);
        }}
        onCommitted={routeCommitted}
        commitReconciliation={routeCommitReconciliation}
        onObserved={routeObserved}
        readAgents={readRouteAgents}
        readSources={readRouteSources}
        onDirectMode={(attempt, observedAgent) => {
          const landingBackend = attempt?.backend ?? routeTarget?.agent.backend;
          if (landingBackend) {
            suspendedSourceBaselinesRef.current.set(landingBackend, sourcesRead);
            setSuspendedRouteAttempts((attempts) =>
              attempt
                ? holdSuspendedRouteAttempt(attempts, attempt)
                : releaseSuspendedRouteAttempt(attempts, landingBackend),
            );
          }
          const target = routeTarget;
          setRouteTarget(null);
          if (target) focusRouteDestination(target);
          void (async () => {
            let landing = observedAgent;
            if (!landing) {
              try {
                const observation = await readRouteAgents();
                observation.install();
                landing = observation.value.find((row) => row.backend === landingBackend) ?? null;
              } catch {
                setSupplyRead(failRegionRead);
                return;
              }
            }
            if (landing?.mode !== 'hub') return;
            try {
              const sourceObservation = await readRouteSources();
              sourceObservation.install();
            } catch {
              setSourcesRead(failRegionRead);
            }
          })();
        }}
      />
      {adoptAgent && (
        <EnableGatewayDialog
          key={adoptAgent.backend}
          agent={adoptAgent}
          runtime={runtimeRead}
          agentReads={agentCollectionReads}
          onClose={() => setAdoptAgent(null)}
          onAdopted={agentSaved}
          onRuntime={(next) => {
            setRuntimeRead((previous) => next === null ? failRegionRead(previous) : readyRegion(next));
          }}
          trackWrite={(work) => agentWriteRegistry.track(adoptAgent.backend, work)}
        />
      )}
      {installOpen && retainedRuntime && (
        <InstallGatewayDialog
          runtime={retainedRuntime}
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
