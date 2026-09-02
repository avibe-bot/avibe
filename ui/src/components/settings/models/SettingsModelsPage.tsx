import * as React from 'react';
import { Gauge, LoaderCircle, Power, RefreshCw, Route, ScrollText } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover';
import { useToast } from '@/context/ToastContext';
import { cn } from '@/lib/utils';
import { ToggleSwitch } from '../SettingsPrimitives';
import { AddApiKeyDialog } from './AddApiKeyDialog';
import { BackendModelCatalogDialog } from './BackendModelCatalogDialog';
import { OAuthConnectDialog } from './OAuthConnectDialog';
import { EnableGatewayDialog } from './EnableGatewayDialog';
import { GatewayModule } from './GatewayModule';
import { InstallGatewayDialog } from './InstallGatewayDialog';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import { RecentSwitchesCard } from './RecentSwitchesCard';
import { RouteChainDialog, type RouteCollectionObservation, type RouteCommitReconciliation, type RouteReport } from './RouteChainDialog';
import { routeChainMatchesAttempt } from './routeChainDraft';
import { SourceDetailPanel } from './SourceDetailPanel';
import { SourceMutationReport } from './SourceMutationReport';
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
import { UsageTab } from './UsageTab';
import './modelHubSurface.css';
import { agentsWithEcho, createLatestAsyncAuthority, createLatestAsyncAuthorityByKey, createLatestEntityAuthorityByKey, createPendingWrites, mapWithConcurrency } from './asyncLifetime';
import { createAgentCollectionReadAuthority, createSourceCollectionReadAuthority } from './collectionReadAuthority';
import { emptyFeed, feedAfterHeadRead, feedAfterTailRead, feedTailCursor, type EventFeed } from './eventFeed';
import { modelsApi, type SourceCreated } from './modelsApi';
import { convergeMutation, createIntentAuthority } from './mutationConvergence';
import {
  readSurfaceLanding,
  sourceMutationLanding,
  type SourceMutationLanding,
  type SourceMutationLandingReads,
  type SourceMutationSettlement,
  type TrackSourceMutation,
} from './mutationSettlement';
import { modelChainKey, modelChainRequests, type ModelChainIndex, type ModelChainRequest } from './modelRows';
import {
  beginRegionRead,
  failRegionRead,
  foldRegionRead,
  degradedRegion,
  loadingRegion,
  readRegion,
  readyRegion,
  settleRegionRead,
  unreadRegion,
  type RegionRead,
} from './regionRead';
import { freshRuntimeProjection, pollRuntimeStatus, runtimeCanAttemptInstall, runtimeIsRunning, startRuntimeWithStatusRefresh } from './runtimeLifecycle';
import { createRouteProjectionReconciler, type RouteProjectionStatus } from './routeProjectionReconciliation';
import { useSourceMutationReport } from './useSourceMutationReport';
import { backendVisual } from './vendorMeta';
import { USAGE_DEFAULT_WINDOW_DAYS, type AgentBackend, type AgentSupply, type ResolutionEvent, type RuntimeDependency, type Source, type UsageSummary } from './types';
import type { UsageWindowOption } from './usageProjection';

const CHAIN_READ_CONCURRENCY = 6;
const EVENT_PAGE = 20;
const SUBSCRIPTION_PICKER_OPTIONS = [
  { vendor: 'anthropic', recommendation: 'native' },
  { vendor: 'openai', recommendation: 'gateway' },
] as const;
type SubscriptionPickerVendor = (typeof SUBSCRIPTION_PICKER_OPTIONS)[number]['vendor'];

const readChainRequests = async (requests: readonly ModelChainRequest[]): Promise<ModelChainIndex> => Object.fromEntries(
  await mapWithConcurrency(
    requests,
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

const readAgentChains = async (agent: AgentSupply): Promise<ModelChainIndex> => {
  const requests = modelChainRequests([agent]);
  try {
    const chains = new Map(
      (await modelsApi.getAgentChains(agent.backend)).map((chain) => [chain.model_id, chain]),
    );
    return Object.fromEntries(requests.map(({ backend, modelId }) => {
      const chain = chains.get(modelId);
      return [
        modelChainKey(backend, modelId),
        chain?.backend === backend ? readyRegion(chain) : unreadRegion(),
      ];
    }));
  } catch {
    return Object.fromEntries(requests.map(({ backend, modelId }) => [
      modelChainKey(backend, modelId),
      unreadRegion(),
    ]));
  }
};

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

type ChainAuthorityLanding =
  | { scope: 'backend'; agent: AgentSupply; chains: ModelChainIndex }
  | { scope: 'models'; chains: ModelChainIndex };

type AuthorizedSurfaceLanding = {
  landing: SourceMutationLandingReads;
  sourceSnapshot: number;
};

export const RuntimePill: React.FC<{
  read: RegionRead<RuntimeDependency>;
  starting: boolean;
  stopping?: boolean;
  directCount?: number;
}> = ({ read, starting, stopping = false, directCount }) => {
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
  const canInstall = health === 'not_installed' && runtimeCanAttemptInstall(runtime);
  const allDirect = authoritative && !starting && !stopping && health === 'ok' && directCount !== undefined && directCount > 0;
  const key = unread
    ? 'unread'
    : stopping
    ? 'stopping'
    : starting
    ? 'starting'
    : health === 'installing'
      ? 'starting'
    : runtime.enabled && !runtimeIsRunning(runtime)
      ? 'unavailable'
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
  const className = cn(
    'model-hub-runtime-pill',
    (health === 'down' || health === 'degraded' || unread) && 'model-hub-runtime-pill--error',
    allDirect && 'model-hub-runtime-pill--direct',
  );
  return <span className={className}><span className="model-hub-runtime-dot" />{(starting || stopping || health === 'installing') && <LoaderCircle className="animate-spin" />}{t(`settings.models.shell.${key}`, allDirect ? { count: directCount } : undefined)}</span>;
};

const RuntimeClosedState: React.FC<{
  read: RegionRead<RuntimeDependency>;
  runtime: RuntimeDependency | null;
  starting: boolean;
  stopping: boolean;
}> = ({ read, runtime, starting, stopping }) => {
  const { t } = useTranslation();
  const health = runtime?.status.health ?? null;
  const key = stopping
    ? 'stopping'
    : starting
      ? 'starting'
      : read.kind === 'unread'
        ? 'unread'
        : health === 'installing'
          ? 'installing'
          : runtime?.enabled && health !== null && !runtimeIsRunning(runtime)
            ? 'enabledDown'
          : health === 'not_installed' && runtime?.manifest.resolution === 'unsupported'
            ? 'unsupported'
            : health === 'not_installed'
              ? 'notInstalled'
              : health === 'down'
                ? 'down'
                : 'off';
  const busy = starting || stopping || health === 'installing';
  return (
    <section className="model-hub-runtime-closed" aria-live="polite">
      <span className="model-hub-runtime-closed-icon" aria-hidden="true">
        {busy ? <LoaderCircle className="animate-spin" /> : <Power />}
      </span>
      <h2>{t(`settings.models.shell.closed.${key}.title`)}</h2>
      <p>{t(`settings.models.shell.closed.${key}.body`)}</p>
    </section>
  );
};

const ModelHubShell: React.FC<{ actions?: React.ReactNode; children: React.ReactNode; rootRef?: React.Ref<HTMLDivElement> }> = ({ actions, children, rootRef }) => {
  const { t } = useTranslation();
  return (
    <div ref={rootRef} className="model-hub-shell">
      <header className="model-hub-shell-head">
        <span className="flex items-center gap-[9px]">
          <h1>{t('settings.models.shell.title')}</h1>
          <ModelHubInfoHint
            label={t('settings.models.shell.modelsInfo.label')}
            content={t('settings.models.shell.modelsInfo.body')}
            className="model-hub-shell-info"
          />
        </span>
        {actions}
      </header>
      {children}
    </div>
  );
};

type HubTab = 'sources' | 'usage' | 'logs';

const HubTabs: React.FC<{ tab: HubTab; onChange: (tab: HubTab) => void }> = ({ tab, onChange }) => {
  const { t } = useTranslation();
  return (
    <div role="tablist" className="flex h-[39px] items-end gap-1 border-b border-border">
      {(['sources', 'usage', 'logs'] as const).map((id) => (
        <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => onChange(id)} className={cn('flex h-[41px] items-center gap-[7px] border-b-2 px-3.5 text-[13px] transition-colors', tab === id ? 'border-mint font-semibold text-foreground' : 'border-transparent font-normal text-muted hover:text-foreground')}>
          {id === 'sources' ? <Route className="size-3.5" /> : id === 'usage' ? <Gauge className="size-3.5" /> : <ScrollText className="size-3.5" />}
          {t(`settings.models.shell.tab.${id === 'sources' ? 'hub' : id}`)}
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
                      <span className="model-hub-pill model-hub-direct-kind-pill border">{t('settings.models.direct.pill.direct')}</span>
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
  const [tab, setTab] = React.useState<HubTab>('sources');
  const [usageRead, setUsageRead] = React.useState<RegionRead<UsageSummary>>(loadingRegion);
  const [usageWindow, setUsageWindow] = React.useState<UsageWindowOption>(USAGE_DEFAULT_WINDOW_DAYS);
  const [startingRuntime, setStartingRuntime] = React.useState(false);
  const [stoppingRuntime, setStoppingRuntime] = React.useState(false);
  const [runtimeRecoveryPending, setRuntimeRecoveryPending] = React.useState(false);
  const [installOpen, setInstallOpen] = React.useState(false);
  const [apiKeyOpen, setApiKeyOpen] = React.useState(false);
  const [subscriptionPickerOpen, setSubscriptionPickerOpen] = React.useState(false);
  const [subscriptionPickerIndex, setSubscriptionPickerIndex] = React.useState(0);
  const [subscriptionVendor, setSubscriptionVendor] = React.useState<string | null>(null);
  // The source a re-login is FOR, held as the snapshot `OAuthConnectDialog`
  // documents its `reauth` prop to be: a native re-auth writes 需处理 onto the row
  // before the login starts, so re-reading the live row mid-flow would rewrite
  // the dialog's own subject. Separate from `subscriptionVendor` because the two
  // journeys start from different surfaces — this one from the source detail,
  // which replaces the overview that holds 添加订阅 — and only the create path
  // owns the success-landing timer and reconcile flag below.
  const [reauthSource, setReauthSource] = React.useState<Source | null>(null);
  const subscriptionTriggerRef = React.useRef<HTMLButtonElement>(null);
  const apiKeyTriggerRef = React.useRef<HTMLButtonElement | null>(null);
  const subscriptionAnchorRef = subscriptionTriggerRef as React.RefObject<HTMLButtonElement>;
  const subscriptionPickerRefs = React.useRef<Partial<Record<SubscriptionPickerVendor, HTMLButtonElement | null>>>({});
  const subscriptionPickerHandoffRef = React.useRef(false);
  const subscriptionCloseTimer = React.useRef<number | null>(null);
  // A successful OAuth terminal reports the same moved rows twice: first with
  // the created source, then as the generic stale-row notification. The source
  // callback owns the one full reconciliation; consume the trailing notification
  // so it cannot launch a second refresh that overwrites a successful landing.
  const subscriptionSuccessReconcileRef = React.useRef(false);
  const sourceDetailHeadingRef = React.useRef<HTMLHeadingElement>(null);
  const sourceDetailReturnFocusRef = React.useRef<(() => HTMLElement | null) | null>(null);
  const [orderBackend, setOrderBackend] = React.useState<AgentBackend | null>(null);
  const [menuBackend, setMenuBackend] = React.useState<AgentBackend | null>(null);
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
  const [presenceRefreshing, setPresenceRefreshing] = React.useState(false);
  const sourceMutationReport = useSourceMutationReport();
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
  const runtimeRunning = retainedRuntime !== null
    && runtimeIsRunning(retainedRuntime)
    && runtimeRead.kind !== 'unread';
  const runtimeEnabled = retainedRuntime !== null
    && (retainedRuntime.enabled ?? runtimeIsRunning(retainedRuntime))
    && runtimeRead.kind !== 'unread';
  const runtimeConfigurationVisible = (
    runtimeRunning || (runtimeEnabled && runtimeHealth !== 'installing')
  ) && !stoppingRuntime;
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

  const [chainReadAuthority] = React.useState(() => createLatestAsyncAuthorityByKey<AgentBackend, ChainAuthorityLanding>((_backend, incoming) => {
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

  const refreshAffectedChains = React.useCallback(async (
    requests: readonly ModelChainRequest[],
  ): Promise<ModelChainIndex> => {
    const byBackend = new Map<AgentBackend, ModelChainRequest[]>();
    for (const request of requests) {
      const backendRequests = byBackend.get(request.backend) ?? [];
      backendRequests.push(request);
      byBackend.set(request.backend, backendRequests);
    }
    const landings = await Promise.all([...byBackend].map(async ([backend, backendRequests]) => {
      let incoming: ModelChainIndex = {};
      const result = await chainReadAuthority.run(backend, async () => {
        incoming = await readChainRequests(backendRequests);
        return { scope: 'models' as const, chains: incoming };
      });
      return result === 'landed'
        ? incoming
        : Object.fromEntries(backendRequests.map(({ backend: requestBackend, modelId }) => [
            modelChainKey(requestBackend, modelId),
            unreadRegion(),
          ]));
    }));
    return Object.assign({}, ...landings);
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

  const [usageReadAuthority] = React.useState(() => createLatestAsyncAuthority<RegionRead<UsageSummary>>((incoming) => {
    if (!aliveRef.current) return;
    setUsageRead((previous) => settleRegionRead(previous, incoming));
  }));

  const refreshUsage = React.useCallback(async (days: UsageWindowOption) => {
    setUsageRead(beginRegionRead);
    await usageReadAuthority.run(() => readRegion(() => modelsApi.getUsageSummary(days)));
  }, [usageReadAuthority]);

  /**
   * The usage report is read lazily, when the tab is opened.
   *
   * It is deliberately NOT a first-paint region (see `firstPaintRegions.ts`,
   * whose whitelist is a policy list of exactly the three reads the landing
   * cannot be drawn without): a report nobody is looking at must not delay the
   * surface that decides routing. Re-reading on every open is the point — the
   * figure is live, and `beginRegionRead` keeps the previous one on screen while
   * the new one lands, so returning to the tab never flashes empty. A window
   * change is the same read with a different span, which is why one effect owns
   * both.
   */
  React.useEffect(() => {
    if (tab !== 'usage') return;
    void refreshUsage(usageWindow);
  }, [tab, usageWindow, refreshUsage]);

  const retryUsage = React.useCallback(async () => {
    await refreshUsage(usageWindow);
  }, [refreshUsage, usageWindow]);

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

  React.useEffect(() => {
    if (tab !== 'logs') return;
    void refreshEventHead();
  }, [refreshEventHead, tab]);

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

  const refresh = React.useCallback(async (
    affectedChains: ModelChainRequest[] = [],
  ): Promise<SourceMutationLanding> => {
    const outcome: { landing: SourceMutationLandingReads | null } = { landing: null };
    const result = await refreshAuthority.run(async () => {
      const sourceSnapshot = sourceEntityAuthority.beginSnapshot();
      outcome.landing = await readSurfaceLanding({
        sources: sourceCollectionReads.readValue,
        supply: agentCollectionReads.readValue,
        runtime: () => modelsApi.getRuntimeStatus(),
        chains: refreshAffectedChains,
      }, affectedChains);
      return { landing: outcome.landing, sourceSnapshot };
    });
    const landing = sourceMutationLanding(
      outcome.landing,
      affectedChains,
      aliveRef.current && result === 'landed',
    );
    if (aliveRef.current && result === 'landed' && landing.verdict === 'degraded') {
      showToast(t('settings.models.toast.refreshFailed') as string, 'error');
    }
    return landing;
  }, [agentCollectionReads, refreshAffectedChains, refreshAuthority, showToast, sourceCollectionReads, sourceEntityAuthority, t]);

  const trackSourceMutation = React.useCallback((sourceId: string): TrackSourceMutation => async <T,>(work: (source: Source, settlement: SourceMutationSettlement) => Promise<T>): Promise<T> => {
    let result!: T;
    await sourceWriteRegistry.track(sourceId, async () => {
      const current = sourceEntityAuthority.current(sourceId);
      if (!current) throw new Error(`Source ${sourceId} is no longer available`);
      const generation = sourceEntityAuthority.begin(sourceId);
      let settled = false;
      const finish = async (
        apply: () => void,
        affectedChains: ModelChainRequest[] = [],
      ): Promise<SourceMutationLanding> => {
        if (!settled) {
          settled = true;
          apply();
        }
        return refresh(affectedChains);
      };
      const settlement: SourceMutationSettlement = {
        source: async (echoed, scope) => finish(
          () => { sourceEntityAuthority.settle(generation, echoed); },
          scope?.affectedChains,
        ),
        gone: async (goneId, inventory, scope) => finish(() => {
          if (inventory) {
            sourceEntityAuthority.settleSnapshotEntries(
              inventory.snapshot,
              inventory.sources.filter((source) => source.id !== goneId),
            );
          }
          if (goneId === sourceId) sourceEntityAuthority.settleRemoval(generation);
        }, scope?.affectedChains),
        unread: async (scope) => finish(
          () => { sourceEntityAuthority.abandon(generation); },
          scope?.affectedChains,
        ),
        release: () => {
          if (settled) return;
          settled = true;
          sourceEntityAuthority.abandon(generation);
        },
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

  const refreshAgentPresence = React.useCallback(async () => {
    setPresenceRefreshing(true);
    try {
      const result = await agentCollectionReads.refresh();
      if (!aliveRef.current || result.kind === 'stale') return;
      setSupplyRead(readyRegion(result.value));
    } finally {
      if (aliveRef.current) setPresenceRefreshing(false);
    }
  }, [agentCollectionReads]);

  React.useEffect(() => {
    let current = true;
    void refresh().then(() => {
      if (!current) return;
      void refreshAgentPresence().catch(() => {
        // The fast snapshot remains authoritative if optional deep discovery fails.
      });
    });
    return () => { current = false; };
  }, [refresh, refreshAgentPresence]);

  const retrySources = React.useCallback(async () => {
    setSourcesRead(beginRegionRead);
    await refresh();
  }, [refresh]);

  const retrySupply = React.useCallback(async () => {
    if (presenceRefreshing) return;
    try {
      await refreshAgentPresence();
    } catch {
      if (aliveRef.current) setSupplyRead(failRegionRead);
    }
  }, [presenceRefreshing, refreshAgentPresence]);

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
    if (startingRuntime || stoppingRuntime) return;
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
  const stopRuntime = async () => {
    if (startingRuntime || stoppingRuntime) return;
    setStoppingRuntime(true);
    try {
      const stopped = await modelsApi.stopRuntime();
      setRuntimeRead(readyRegion(stopped));
      setRuntimeRecoveryPending(false);
      setSelectedSourceId(null);
      setTab('sources');
    } catch {
      const observed = await modelsApi.getRuntimeStatus().catch(() => null);
      setRuntimeRead((previous) => observed ? readyRegion(observed) : failRegionRead(previous));
      showToast(t('settings.models.errors.stopFailed') as string, 'error');
    } finally {
      setStoppingRuntime(false);
    }
  };
  const landingLoading = sourcesRead.kind === 'loading'
    && supplyRead.kind === 'loading'
    && runtimeRead.kind === 'loading';
  const directEmpty = modelsSurfaceKindFromReads(supplyRead, sourcesRead) === 'direct_empty';
  const installedAgents = agents.filter((agent) => agent.cli_present);
  const hubBackends = agents.filter((agent) => agent.mode === 'hub').map((agent) => agent.backend);
  const activeBackends = supplyRead.kind === 'ready' ? new Set(hubBackends) : undefined;
  const stopBlocked = runtimeEnabled && (supplyRead.kind !== 'ready' || hubBackends.length > 0);
  const runtimeSwitchUnsupported = !runtimeEnabled
    && runtimeHealth === 'not_installed'
    && retainedRuntime?.manifest.resolution === 'unsupported';
  const runtimeSwitchDisabled = startingRuntime
    || stoppingRuntime
    || runtimeRead.kind !== 'ready'
    || runtimeHealth === 'installing'
    || runtimeSwitchUnsupported
    || stopBlocked;
  const runtimeSwitchLabel = stopBlocked
    ? supplyRead.kind === 'ready'
      ? t('settings.models.shell.toggle.stopBlocked', { names: hubBackends.join(', ') })
      : t('settings.models.shell.toggle.stopUnavailable')
    : runtimeEnabled
      ? t('settings.models.shell.toggle.turnOff')
      : t('settings.models.shell.toggle.turnOn');
  const toggleRuntime = () => {
    if (runtimeSwitchDisabled) return;
    if (runtimeEnabled) {
      void stopRuntime();
    } else if (runtimeHealth === 'not_installed') {
      setInstallOpen(true);
    } else {
      void startRuntime();
    }
  };
  const installedSupplyRead = foldRegionRead<AgentSupply[], RegionRead<AgentSupply[]>>(supplyRead, {
    loading: () => loadingRegion(),
    ready: () => readyRegion(installedAgents),
    unread: (retryable) => unreadRegion(retryable),
    degraded: (_staleData, cause, retryable) => degradedRegion(installedAgents, cause, retryable),
  });
  type SourceDetailSelection = {
    sourceId: string;
    returnFocus: () => HTMLElement | null;
  };
  const applySourceDetailSelection = React.useCallback((selection: SourceDetailSelection | null) => {
    if (selection) sourceDetailReturnFocusRef.current = selection.returnFocus;
    setSelectedSourceId(selection?.sourceId ?? null);
  }, []);
  const selectSource = React.useCallback((selection: SourceDetailSelection | null) => {
    sourceIntentAuthority.commit(() => applySourceDetailSelection(selection));
  }, [applySourceDetailSelection, sourceIntentAuthority]);
  const selectedSource = sources.find((source) => source.id === selectedSourceId) ?? null;
  const sourceDetailOpen = selectedSourceId !== null && subscriptionVendor === null;
  const orderAgent = agents.find((agent) => agent.backend === orderBackend && agent.mode === 'hub') ?? null;
  const menuAgent = agents.find((agent) => agent.backend === menuBackend && agent.mode === 'hub') ?? null;
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
            chains: await readExactAgentChain(freshAgent, held.modelId),
            scope: 'models' as const,
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
          applySourceDetailSelection({
            sourceId: created.source.id,
            returnFocus: () => apiKeyTriggerRef.current,
          });
        },
      },
      reconcile: refresh,
    });
  };
  const subscriptionAdded = React.useCallback((source?: Source) => {
    if (!source) {
      if (subscriptionSuccessReconcileRef.current) {
        subscriptionSuccessReconcileRef.current = false;
        return;
      }
      void refresh();
      return;
    }
    // Keep the success panel as the only active dialog until its handoff timer
    // closes it. The provider dialog then opens and owns its normal autofocus.
    sourceEntityAuthority.landLatest(source);
    selectSource({
      sourceId: source.id,
      returnFocus: () => subscriptionTriggerRef.current,
    });
    subscriptionSuccessReconcileRef.current = true;
    void refresh();
    if (subscriptionCloseTimer.current !== null) window.clearTimeout(subscriptionCloseTimer.current);
    subscriptionCloseTimer.current = window.setTimeout(() => {
      subscriptionCloseTimer.current = null;
      setSubscriptionVendor(null);
    }, 1400);
  }, [refresh, selectSource, sourceEntityAuthority]);
  const closeSubscription = React.useCallback(() => {
    if (subscriptionCloseTimer.current !== null) {
      window.clearTimeout(subscriptionCloseTimer.current);
      subscriptionCloseTimer.current = null;
    }
    setSubscriptionVendor(null);
    window.setTimeout(() => subscriptionTriggerRef.current?.focus(), 0);
  }, []);
  /**
   * A re-login's whole obligation to this page: re-read everything.
   *
   * Not `subscriptionAdded`, even though the reauth journey also calls back with
   * no source. That callback's no-source branch can be CONSUMED by the create
   * path's success flag, and a reauth has no success landing to coalesce with —
   * it terminates on the row it started from. And the read has to be the wide
   * one: `_materialize_reauth` can leave other agents without a source, so
   * `/agents` and the chains behind it are stale too, not just this row.
   */
  const sourceReauthed = React.useCallback(() => { void refresh(); }, [refresh]);
  const closeReauth = React.useCallback(() => {
    setReauthSource(null);
    // Back to the detail heading rather than to the button that opened this: a
    // repair that worked unmounts that button (the row is no longer stopped), and
    // Radix would restore focus to a node that is gone — i.e. to <body>.
    window.setTimeout(() => sourceDetailHeadingRef.current?.focus(), 0);
  }, []);
  const closeSubscriptionPicker = React.useCallback(() => {
    subscriptionPickerHandoffRef.current = false;
    setSubscriptionPickerOpen(false);
  }, []);
  const focusSubscriptionPickerOption = React.useCallback((index: number) => {
    const bounded = Math.max(0, Math.min(index, SUBSCRIPTION_PICKER_OPTIONS.length - 1));
    setSubscriptionPickerIndex(bounded);
    const { vendor } = SUBSCRIPTION_PICKER_OPTIONS[bounded];
    subscriptionPickerRefs.current[vendor]?.focus();
  }, []);
  const openSubscriptionPicker = React.useCallback(() => {
    subscriptionPickerHandoffRef.current = false;
    setSubscriptionPickerIndex(0);
    setSubscriptionPickerOpen(true);
  }, []);
  const toggleSubscriptionPicker = React.useCallback(() => {
    if (subscriptionPickerOpen) closeSubscriptionPicker();
    else openSubscriptionPicker();
  }, [closeSubscriptionPicker, openSubscriptionPicker, subscriptionPickerOpen]);
  React.useEffect(() => () => {
    if (subscriptionCloseTimer.current !== null) window.clearTimeout(subscriptionCloseTimer.current);
  }, []);

  return (
    <ModelHubShell
      rootRef={pageRef}
      actions={!landingLoading
        ? <span className="flex items-center gap-2">
              <RuntimePill
                read={runtimeRead}
                starting={startingRuntime}
                stopping={stoppingRuntime}
                directCount={directEmpty ? installedAgents.length : undefined}
              />
              {runtimeConfigurationVisible && !directEmpty && <TakeoverPill count={takeoverCount} />}
              {runtimeConfigurationVisible && <Button
                type="button"
                variant="ghost"
                size="icon"
                className="size-8"
                disabled={presenceRefreshing}
                aria-label={t('settings.models.direct.action.refreshAgents')}
                title={t('settings.models.direct.action.refreshAgents')}
                onClick={() => void retrySupply()}
              ><RefreshCw aria-hidden className={cn('size-3.5', presenceRefreshing && 'animate-spin')} /></Button>}
              <span title={runtimeSwitchLabel}>
                <ToggleSwitch
                  enabled={runtimeEnabled}
                  disabled={runtimeSwitchDisabled}
                  label={runtimeSwitchLabel}
                  onClick={toggleRuntime}
                />
              </span>
            </span>
        : undefined}
    >
      {landingLoading ? <div className="text-[13px] text-muted">{t('common.loading')}</div>
        : !runtimeConfigurationVisible
          ? <RuntimeClosedState read={runtimeRead} runtime={retainedRuntime} starting={startingRuntime} stopping={stoppingRuntime} />
          : <div className="space-y-[22px]">
                  {/* The tab strip belongs to the Hub, not to the source
                      inventory. Usage and switch history both outlive the Sources
                      they name, so deleting the last Source may not remove the only
                      route to either record. Frame 09 predates these tabs; it still
                      owns the direct-only body of `sources`. */}
                  <HubTabs tab={tab} onChange={setTab} />
                  {tab === 'usage' ? <UsageTab usage={usageRead} windowDays={usageWindow} onWindowChange={setUsageWindow} onRetry={retryUsage} />
                    : tab === 'logs' ? <RecentSwitchesCard events={eventsRead} sources={sourcesRead} onRetry={retryEvents} loadingMore={loadingEvents} onLoadMore={loadOlderEvents} />
                    : directEmpty ? <DirectHome agents={installedAgents} onSwitch={setAdoptAgent} />
                    : <div className="model-hub-overview">
                    <div className="model-hub-overview-body">
                      <div ref={overviewRef} className="model-hub-overview-grid relative flex flex-col gap-4">
                        <Popover
                          open={subscriptionPickerOpen}
                          onOpenChange={(open) => { if (!open) closeSubscriptionPicker(); }}
                        >
                          <PopoverAnchor virtualRef={subscriptionAnchorRef} />
                          <SourcesCard read={sourcesRead} activeBackends={activeBackends} readFailureCopy={routeCommitStatus?.failed.has('sources') ? t('settings.models.routeDialog.impact.refreshFail') : undefined} onRetry={() => routeCommitStatus?.failed.has('sources') ? retryRouteCommit() : void retrySources()} onOpenSource={(source, opener) => selectSource({ sourceId: source.id, returnFocus: () => opener })} onAddApiKey={(opener) => { apiKeyTriggerRef.current = opener; setApiKeyOpen(true); }} onAddSubscription={toggleSubscriptionPicker} subscriptionPickerOpen={subscriptionPickerOpen} subscriptionTriggerRef={subscriptionTriggerRef} />
                          <PopoverContent
                            role="menu"
                            aria-label={t('settings.models.upstream.addSubscription')}
                            align="start"
                            sideOffset={8}
                            collisionPadding={12}
                            className="flex w-[300px] max-w-[calc(100vw-24px)] flex-col gap-0.5 rounded-[10px] border-border-strong bg-surface p-1.5 text-foreground !shadow-[var(--model-hub-menu-shadow)]"
                            onOpenAutoFocus={(event) => {
                              event.preventDefault();
                              window.requestAnimationFrame(() => {
                                const { vendor } = SUBSCRIPTION_PICKER_OPTIONS[subscriptionPickerIndex];
                                subscriptionPickerRefs.current[vendor]?.focus();
                              });
                            }}
                            onCloseAutoFocus={(event) => {
                              event.preventDefault();
                              if (subscriptionPickerHandoffRef.current) {
                                subscriptionPickerHandoffRef.current = false;
                                return;
                              }
                              window.setTimeout(() => subscriptionTriggerRef.current?.focus(), 0);
                            }}
                            onInteractOutside={(event) => {
                              if (subscriptionTriggerRef.current?.contains(event.target as Node)) event.preventDefault();
                            }}
                            onKeyDown={(event) => {
                              if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
                                event.preventDefault();
                                focusSubscriptionPickerOption(subscriptionPickerIndex + 1);
                              } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
                                event.preventDefault();
                                focusSubscriptionPickerOption(subscriptionPickerIndex - 1);
                              } else if (event.key === 'Home') {
                                event.preventDefault();
                                focusSubscriptionPickerOption(0);
                              } else if (event.key === 'End') {
                                event.preventDefault();
                                focusSubscriptionPickerOption(SUBSCRIPTION_PICKER_OPTIONS.length - 1);
                              }
                            }}
                          >
                            {SUBSCRIPTION_PICKER_OPTIONS.map(({ vendor, recommendation }, index) => {
                              const vendorLabel = t(`settings.models.subscriptionPicker.vendor.${vendor}`);
                              const recommendationLabel = t(`settings.models.subscriptionPicker.recommendation.${recommendation}`);
                              return (
                                <Button
                                  key={vendor}
                                  ref={(node) => { subscriptionPickerRefs.current[vendor] = node; }}
                                  type="button"
                                  role="menuitem"
                                  variant="ghost"
                                  className={cn(
                                    'model-hub-subscription-menu-row h-auto w-full min-w-0 justify-start gap-2 rounded-[7px] px-2.5 py-[9px] text-left focus-visible:ring-0 focus-visible:ring-offset-0',
                                    subscriptionPickerIndex === index &&
                                      'model-hub-subscription-menu-row--active',
                                  )}
                                  tabIndex={subscriptionPickerIndex === index ? 0 : -1}
                                  onClick={() => {
                                    subscriptionPickerHandoffRef.current = true;
                                    setSubscriptionPickerOpen(false);
                                    setSubscriptionVendor(vendor);
                                  }}
                                >
                                  <span className="min-w-0 flex-1 truncate text-[12.5px] font-semibold" title={vendorLabel}>{vendorLabel}</span>
                                  <Badge
                                    variant="recommendation"
                                    className={cn(
                                      recommendation === 'native'
                                        ? 'model-hub-accent-tile--mint model-hub-accent-ink--mint border-transparent'
                                        : 'model-hub-accent-pill--neutral',
                                    )}
                                  >
                                    {recommendationLabel}
                                  </Badge>
                                </Button>
                              );
                            })}
                          </PopoverContent>
                        </Popover>
                        <div className="hidden xl:block" aria-hidden="true" />
                        <GatewayModule supply={installedSupplyRead} readFailureCopy={routeCommitStatus?.failed.has('agents') ? t('settings.models.routeDialog.impact.refreshFail') : undefined} sources={sources} chains={chains} runtime={runtime} runtimeSnapshot={retainedRuntime} onRetry={() => routeCommitStatus?.failed.has('agents') ? retryRouteCommit() : void retrySupply()} pendingBackends={agentWrites} switchFailures={switchFailures} connectingBackend={adoptAgent?.backend ?? null} onConnectHub={setAdoptAgent} onSwitchDirect={switchToDirect} onOpenModels={(agent) => setMenuBackend(agent.backend)} onOpenOrder={(agent) => setOrderBackend(agent.backend)} onOpenRoute={(agent, modelId, opener) => setRouteTarget({ agent, modelId, opener })} onProbeSettled={(agent) => void refreshAgentChains(agent)} />
                        <SupplyGraph containerRef={overviewRef} relations={supplyRelations} />
                      </div>
                      <SupplyLegend relations={supplyRelations} />
                    </div>
                  </div>}
                </div>}
      {runtimeConfigurationVisible && <>
      <Dialog open={sourceDetailOpen} onOpenChange={(open) => { if (!open) selectSource(null); }}>
        <DialogContent
          mobileSheetHeight="tall"
          closeLabel={t('settings.models.sourceDetail.close') as string}
          className="model-hub-source-dialog flex h-[min(624px,calc(100dvh-32px))] w-[min(720px,calc(100vw-32px))] max-w-[720px] flex-col gap-0 overflow-hidden rounded-[14px] border-border-strong bg-surface p-0 shadow-[var(--model-hub-dialog-shadow)] max-md:w-full max-md:max-w-none max-md:rounded-t-2xl"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            sourceDetailHeadingRef.current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            const returnFocus = sourceDetailReturnFocusRef.current;
            sourceDetailReturnFocusRef.current = null;
            const target = returnFocus?.();
            if (!target?.isConnected) return;
            event.preventDefault();
            target.focus();
          }}
          onEscapeKeyDown={(event) => {
            // Radix observes Escape before React's row handlers; marked editors own it locally.
            if (event.target instanceof Element && event.target.closest('[data-source-dialog-local-escape]')) {
              event.preventDefault();
            }
          }}
        >
          <DialogTitle className="sr-only">{selectedSource?.display_name ?? t('settings.models.sourceDetail.gone')}</DialogTitle>
          <DialogDescription className="sr-only">{t('settings.models.sourceDetail.footnote')}</DialogDescription>
          {selectedSource
            ? <SourceDetailPanel
                key={selectedSource.id}
                source={selectedSource}
                activeBackends={activeBackends}
                headingRef={sourceDetailHeadingRef}
                trackMutation={trackSourceMutation(selectedSource.id)}
                onReauth={setReauthSource}
                onMutationCommitted={sourceMutationReport.present}
              />
            : <section className="grid min-h-0 flex-1 place-items-center px-5 py-12 text-center text-[12px] text-muted">{t('settings.models.sourceDetail.gone')}</section>}
        </DialogContent>
      </Dialog>
      <SourceMutationReport
        report={sourceMutationReport.report}
        onComplete={() => { void sourceMutationReport.complete(); }}
        onDismiss={sourceMutationReport.dismiss}
      />
      <AddApiKeyDialog open={apiKeyOpen} sourceReads={sourceCollectionReads} onClose={() => setApiKeyOpen(false)} onAdded={(created) => void sourceAdded(created)} />
      {subscriptionVendor && (
        <OAuthConnectDialog
          open
          vendor={subscriptionVendor}
          sources={sources}
          onClose={closeSubscription}
          onConnected={subscriptionAdded}
        />
      )}
      {reauthSource && (
        <OAuthConnectDialog
          open
          vendor={reauthSource.vendor}
          reauth={reauthSource}
          sources={sources}
          onClose={closeReauth}
          onConnected={sourceReauthed}
        />
      )}
      {orderAgent && <SourceOrderDrawer open agent={orderAgent} sources={sources} sourceReads={sourceCollectionReads} onClose={() => setOrderBackend(null)} onSaved={agentSaved} orderWrite={{ pending: agentWrites.has(orderAgent.backend), track: (work) => agentWriteRegistry.track(orderAgent.backend, work) }} />}
      {menuAgent && <BackendModelCatalogDialog open backend={menuAgent.backend} onClose={() => setMenuBackend(null)} onSaved={agentSaved} onObserved={applyAgentEcho} catalogWrite={{ pending: agentWrites.has(menuAgent.backend), track: (work) => agentWriteRegistry.track(menuAgent.backend, work) }} />}
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
      </>}
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
