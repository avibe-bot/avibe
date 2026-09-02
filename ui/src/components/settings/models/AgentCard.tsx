import * as React from 'react';
import { ArrowDownUp, Check, ChevronDown, ChevronRight, ChevronUp, ListChecks, PlugZap, Power, RefreshCw, Route } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ResponsiveMenu } from '@/components/ui/responsive-menu';
import { cn } from '@/lib/utils';
import { catalogModelIds } from './backendCatalog';
import { collapsedModelRows, modelChainKey, modelSupplyState, type ModelChainIndex, type ModelChainRead } from './modelRows';
import { foldRegionRead } from './regionRead';
import { agentGroupStatus } from './supply';
import { agentHasLiveChainProjection, type FreshRuntimeProjection } from './runtimeLifecycle';
import { currentChainLink, isTakeoverChain } from './takeover';
import { ACCENT_ICON, ACCENT_TILE, backendVisual } from './vendorMeta';
import type { AgentSupply, Source } from './types';

const sourceName = (sources: Source[], id: string): string => sources.find((source) => source.id === id)?.display_name ?? id;
const currentLink = (read: ModelChainRead | undefined) => {
  const chain = read ? foldRegionRead(read, {
    loading: () => null,
    ready: (value) => value,
    unread: () => null,
    degraded: () => null,
  }) : null;
  return chain ? currentChainLink(chain) : null;
};
const isTakeoverRead = (read: ModelChainRead | undefined): boolean => {
  const chain = read ? foldRegionRead(read, {
    loading: () => null,
    ready: (value) => value,
    unread: () => null,
    degraded: () => null,
  }) : null;
  return chain ? isTakeoverChain(chain) : false;
};

const ManageModelsButton: React.FC<{
  className?: string;
  disabled: boolean;
  onClick: () => void;
}> = ({ className, disabled, onClick }) => {
  const { t } = useTranslation();
  return (
    <Button
      variant="outline"
      size="xs"
      className={cn('rounded-md bg-background px-2.5 text-[11px] font-semibold shadow-sm', className)}
      onClick={onClick}
      disabled={disabled}
    >
      <ListChecks aria-hidden="true" />
      {t('settings.models.gateway.manageModels')}
    </Button>
  );
};

const ModelRow: React.FC<{
  agent: AgentSupply;
  modelId: string;
  sources: Source[];
  read: ModelChainRead | undefined;
  onOpenRoute: (agent: AgentSupply, modelId: string, opener: HTMLElement) => void;
}> = ({ agent, modelId, sources, read, onOpenRoute }) => {
  const { t } = useTranslation();
  const current = currentLink(read);
  const takeover = isTakeoverRead(read);
  const supplyState = modelSupplyState(agent, modelId);
  const resolved = read?.kind === 'ready' && current !== null;
  const mode = resolved
    ? current.channel === 'native_cli'
      ? t('settings.models.legend.native') as string
      : t('settings.models.gateway.group.mode.gateway') as string
    : '—';
  const currentSource = resolved ? sourceName(sources, current.source_id) : '';
  const currentCopy = supplyState === 'paused'
    ? t('settings.models.legend.unavailable') as string
    : supplyState === 'unconfigured'
      ? t('settings.models.gateway.group.status.unconfigured') as string
    : resolved
      ? t(takeover ? 'settings.models.gateway.row.currentTakeover' : 'settings.models.gateway.row.current', {
        source: currentSource,
        model: current.model_id,
      }) as string
      : '—';
  const hasCurrentMapping = resolved && supplyState === 'available';
  const openRouteLabel = hasCurrentMapping
    ? t('settings.models.routeDialog.openWithMapping', { model: modelId, mapping: currentCopy }) as string
    : t('settings.models.routeDialog.open', { model: modelId }) as string;
  return (
    <button
      type="button"
      data-route-backend={agent.backend}
      data-route-model={modelId}
      onClick={(event) => onOpenRoute(agent, modelId, event.currentTarget)}
      aria-label={openRouteLabel}
      className="model-hub-model-row flex h-9 w-full min-w-0 items-center gap-2.5 overflow-hidden rounded-md border border-border px-3 text-left"
    >
      <span className="flex min-w-0 flex-1 items-center justify-between gap-2.5">
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] font-medium text-foreground" title={modelId}>{modelId}</span>
        <span className="flex min-w-0 flex-1 items-center justify-end gap-[7px]">
          <span className="model-hub-pill model-hub-model-mode-chip shrink-0 border border-border text-muted">{mode}</span>
          {hasCurrentMapping ? (
            <span
              className={cn('model-hub-model-current min-w-0 flex-1 truncate text-right text-[10.5px]', takeover && 'model-hub-model-current--takeover')}
              title={currentCopy}
              data-route-mapping
            >
              {currentCopy}
            </span>
          ) : (
            <span className={cn('model-hub-model-current min-w-0 flex-1 truncate text-right text-[10.5px]', supplyState === 'paused' && 'model-hub-ink-gold')} title={currentCopy}>{currentCopy}</span>
          )}
        </span>
      </span>
      <ChevronRight className="model-hub-overview-chevron size-[15px] shrink-0" aria-hidden="true" />
    </button>
  );
};

const AgentModelCard: React.FC<{
  agent: AgentSupply;
  runtime: FreshRuntimeProjection | null;
  sources: Source[];
  chains: ModelChainIndex;
  pending: boolean;
  connecting: boolean;
  switchFailed: boolean;
  onConnectHub: (agent: AgentSupply) => void;
  onSwitchDirect: (agent: AgentSupply) => void;
  onOpenModels: (agent: AgentSupply) => void;
  onOpenOrder: (agent: AgentSupply) => void;
  onOpenRoute: (agent: AgentSupply, modelId: string, opener: HTMLElement) => void;
  onProbeSettled: (agent: AgentSupply) => void;
}> = ({ agent, runtime, sources, chains, pending, connecting, switchFailed, onConnectHub, onSwitchDirect, onOpenModels, onOpenOrder, onOpenRoute, onProbeSettled }) => {
  const { t } = useTranslation();
  const { Icon, accent } = backendVisual(agent.backend);
  const [expanded, setExpanded] = React.useState(false);
  const [modeMenuOpen, setModeMenuOpen] = React.useState(false);
  const allModels = catalogModelIds(agent);
  const chainProjectionLive = agentHasLiveChainProjection(runtime, agent);
  const collapsed = collapsedModelRows(agent, expanded);
  const collapsedAtRest = collapsedModelRows(agent);
  const models = collapsed.visible;
  const canCollapse = collapsedAtRest.hidden.length > 0;
  const needsChainRepair = chainProjectionLive
    && allModels.some((modelId) => {
      const read = chains[modelChainKey(agent.backend, modelId)];
      return read?.kind === 'unread' || (read?.kind === 'degraded' && read.cause === 'read_failed');
    });
  const hasTakeover = chainProjectionLive
    && allModels.some((modelId) => isTakeoverRead(chains[modelChainKey(agent.backend, modelId)]));
  const modeWord = t(`settings.models.gateway.group.mode.${agent.mode === 'hub' ? 'gateway' : 'direct'}`) as string;
  const health = agent.mode === 'hub' ? agentGroupStatus(agent.named_agents ?? []) : 'unused';
  const subtitle = agent.mode === 'direct'
    ? t('settings.models.gateway.group.subtitle.direct', { mode: modeWord }) as string
    : t('settings.models.gateway.group.subtitle.gateway', { mode: modeWord, health: t(`settings.models.gateway.group.status.${health}`) }) as string;
  const toggleCollapsed = () => {
    setExpanded((value) => !value);
    onProbeSettled(agent);
  };
  const retryChains = () => onProbeSettled(agent);
  const noUsableSource = agent.mode === 'hub'
    && Boolean(agent.model_supply?.length)
    && agent.model_supply?.every((entry) => entry.chain_length > 0 && !entry.has_runnable_hop);
  const statusClass = switchFailed || health === 'interrupted'
    ? 'text-destructive-ink'
    : hasTakeover || health === 'degraded' || health === 'waiting'
      ? 'model-hub-ink-gold'
      : 'text-muted';
  const statusDot = switchFailed || health === 'interrupted'
    ? 'bg-destructive'
    : hasTakeover || health === 'degraded' || health === 'waiting'
      ? 'bg-gold'
      : health === 'ok' && agent.mode === 'hub'
        ? 'bg-mint'
        : 'bg-muted';
  const modeStatus = switchFailed
    ? t('settings.models.gateway.fail.switchToDirect') as string
    : subtitle;
  return (
    <section className="overflow-hidden rounded-lg border border-border bg-background" data-agent-backend={agent.backend}>
      <div
        tabIndex={-1}
        data-agent-group-head={agent.backend}
        className="flex min-h-[52px] flex-col justify-center border-b border-border px-3.5 py-2"
      >
        <div className="flex min-w-0 flex-col items-stretch gap-2 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between sm:gap-3">
          <div className="flex min-w-0 items-center gap-[9px]">
            <span className={cn('flex size-[30px] shrink-0 items-center justify-center rounded-[9px]', ACCENT_TILE[accent])}><Icon className={cn('size-[15px]', ACCENT_ICON[accent])} /></span>
            <span className="flex min-w-0 items-center gap-[7px]">
              <h2 className="truncate text-[14px] font-bold leading-[17px] text-foreground">{t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend })}</h2>
              <Badge variant="secondary" className={cn('model-hub-pill model-hub-fill-0a', hasTakeover && 'model-hub-takeover-chip')}>
                {hasTakeover
                  ? t('settings.models.takeover.chip')
                  : t('settings.models.gateway.modelCount', { count: allModels.length })}
              </Badge>
            </span>
          </div>
          {agent.mode === 'hub' ? (
            <div className="flex min-w-0 flex-1 flex-wrap items-center justify-end gap-2">
              <ResponsiveMenu
                open={modeMenuOpen}
                onOpenChange={setModeMenuOpen}
                sheetTitle={t('settings.models.gateway.modeMenu.title') as string}
                className="model-hub-mode-menu w-[324px] rounded-[10px] border-border-strong bg-card p-1.5 !shadow-[var(--model-hub-menu-shadow)]"
                trigger={(
                  <button
                    type="button"
                    disabled={pending}
                    aria-label={`${t('settings.models.gateway.modeMenu.title')}: ${modeStatus}`}
                    className={cn('model-hub-agent-mode-trigger flex min-w-0 items-center gap-1.5 rounded-md px-2.5 text-[11px] font-semibold transition-colors hover:text-foreground disabled:opacity-50', statusClass)}
                  >
                    <span className={cn('size-[5px] shrink-0 rounded-full', statusDot)} />
                    <span className="truncate">{modeStatus}</span>
                    <ChevronDown className={cn('size-3 shrink-0 transition-transform', modeMenuOpen && 'rotate-180')} aria-hidden="true" />
                  </button>
                )}
              >
                <p className="model-hub-mode-menu-title px-2.5 pb-1.5 pt-1 text-[10px] font-bold uppercase text-muted max-md:hidden">{t('settings.models.gateway.modeMenu.title')}</p>
                <div role="group" aria-label={t('settings.models.gateway.modeMenu.title') as string}>
                  <button type="button" aria-pressed="true" className="model-hub-mode-menu-current flex w-full items-start gap-2.5 rounded-[7px] px-2.5 py-2.5 text-left" onClick={() => setModeMenuOpen(false)}>
                    <Route className="model-hub-ink-mint mt-0.5 size-4 shrink-0" aria-hidden="true" />
                    <span className="min-w-0 flex-1">
                      <span className="block text-[12px] font-bold text-foreground">{t('settings.models.gateway.modeMenu.gatewayCurrent')}</span>
                      <span className="mt-0.5 block text-[10.5px] leading-[15px] text-muted">{t('settings.models.gateway.modeMenu.gatewayDescription')}</span>
                    </span>
                    <Check className="model-hub-ink-mint mt-0.5 size-4 shrink-0" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    aria-pressed="false"
                    disabled={pending}
                    className="model-hub-mode-menu-item mt-0.5 flex w-full items-start gap-2.5 rounded-[7px] px-2.5 py-2.5 text-left hover:bg-surface-2 disabled:opacity-50"
                    onClick={() => {
                      setModeMenuOpen(false);
                      onSwitchDirect(agent);
                    }}
                  >
                    <Power className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden="true" />
                    <span className="min-w-0">
                      <span className="block text-[12px] font-bold text-foreground">{t(switchFailed ? 'settings.models.gateway.retry' : 'settings.models.gateway.switchToDirect')}</span>
                      <span className="mt-0.5 block text-[10.5px] leading-[15px] text-muted">{t('settings.models.gateway.modeMenu.directDescription', { backend: t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend }) })}</span>
                    </span>
                  </button>
                </div>
              </ResponsiveMenu>
              <ManageModelsButton className="model-hub-agent-head-action" disabled={pending} onClick={() => onOpenModels(agent)} />
              <Button variant="outline" size="xs" className="model-hub-agent-head-action rounded-md bg-background px-2.5 text-[11px] font-semibold shadow-sm" onClick={() => onOpenOrder(agent)} disabled={pending}><ArrowDownUp aria-hidden="true" />{t('settings.models.gateway.sourceOrder')}</Button>
            </div>
          ) : <Button variant="default" size="xs" className="model-hub-agent-head-action shrink-0 self-start rounded-md px-2.5 text-[11px] font-semibold sm:self-auto" onClick={() => onConnectHub(agent)} disabled={connecting}><PlugZap aria-hidden="true" />{t('settings.models.gateway.switchToGateway')}</Button>}
        </div>
      </div>
      {agent.mode === 'hub' && (models.length === 0 ? <div className="flex flex-col items-center gap-3 px-4 py-10 text-center sm:px-5"><p className="text-[12.5px] text-muted">{t('settings.models.gateway.group.emptyModels')}</p><ManageModelsButton disabled={pending} onClick={() => onOpenModels(agent)} /></div> : <div className="space-y-2 p-2">{noUsableSource && <p className="px-3 py-1 text-[11px] font-semibold text-muted">{t('settings.models.gateway.supply.none')}</p>}{models.map((modelId) => <ModelRow key={modelId} agent={agent} modelId={modelId} sources={sources} read={chainProjectionLive ? chains[modelChainKey(agent.backend, modelId)] : undefined} onOpenRoute={onOpenRoute} />)}{canCollapse ? <button type="button" onClick={toggleCollapsed} className="model-hub-model-collapse flex h-6 w-full items-center gap-1.5 hover:text-foreground">{expanded ? <ChevronUp /> : <ChevronDown />}{expanded ? t('settings.models.gateway.collapse') : t('settings.models.gateway.moreModels', { count: collapsed.hidden.length })}</button> : needsChainRepair ? <button type="button" onClick={retryChains} className="model-hub-model-collapse flex h-6 w-full items-center gap-1.5 hover:text-foreground"><RefreshCw />{t('settings.models.gateway.retry')}</button> : null}</div>)}
    </section>
  );
};

export const AgentCard: React.FC<{
  agents: AgentSupply[];
  runtime: FreshRuntimeProjection | null;
  sources: Source[];
  chains: ModelChainIndex;
  pendingBackends: ReadonlySet<string>;
  switchFailures: ReadonlySet<string>;
  onConnectHub: (agent: AgentSupply) => void;
  onSwitchDirect: (agent: AgentSupply) => void;
  onOpenModels: (agent: AgentSupply) => void;
  onOpenOrder: (agent: AgentSupply) => void;
  onOpenRoute: (agent: AgentSupply, modelId: string, opener: HTMLElement) => void;
  onProbeSettled: (agent: AgentSupply) => void;
  connectingBackend: string | null;
}> = ({ agents, pendingBackends, switchFailures, connectingBackend, ...props }) => <div className="flex flex-col gap-2.5">{agents.map((agent) => <AgentModelCard key={agent.backend} agent={agent} {...props} pending={pendingBackends.has(agent.backend)} switchFailed={switchFailures.has(agent.backend)} connecting={connectingBackend === agent.backend} />)}</div>;
