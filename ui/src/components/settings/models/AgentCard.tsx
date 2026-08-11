import * as React from 'react';
import { ChevronDown, ChevronRight, ChevronUp, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import { collapsedModelRows, listedModelIds, modelChainKey, type ModelChainIndex, type ModelChainRead } from './modelRows';
import { agentHasLiveChainProjection } from './runtimeLifecycle';
import { currentChainLink, isTakeoverChain } from './takeover';
import { ACCENT_ICON, ACCENT_TILE, backendVisual } from './vendorMeta';
import type { AgentSupply, RuntimeDependency, Source } from './types';

const sourceName = (sources: Source[], id: string): string => sources.find((source) => source.id === id)?.display_name ?? id;
const currentLink = (read: ModelChainRead | undefined) => read?.kind === 'ready' ? currentChainLink(read.data) : null;
const isTakeoverRead = (read: ModelChainRead | undefined): boolean => read?.kind === 'ready' && isTakeoverChain(read.data);

const ModelRow: React.FC<{
  agent: AgentSupply;
  modelId: string;
  sources: Source[];
  read: ModelChainRead | undefined;
  onOpenRoute: (agent: AgentSupply, modelId: string) => void;
}> = ({ agent, modelId, sources, read, onOpenRoute }) => {
  const { t } = useTranslation();
  const current = currentLink(read);
  const takeover = isTakeoverRead(read);
  const resolved = read?.kind === 'ready' && current !== null;
  const mode = resolved
    ? current.channel === 'native_cli'
      ? t('settings.models.legend.native') as string
      : t('settings.models.gateway.group.mode.gateway') as string
    : '—';
  const currentCopy = resolved
    ? t(takeover ? 'settings.models.gateway.row.currentTakeover' : 'settings.models.gateway.row.current', { source: sourceName(sources, current.source_id) }) as string
    : '—';
  return (
    <button
      type="button"
      onClick={() => onOpenRoute(agent, modelId)}
      aria-label={t('settings.models.routeDialog.open', { model: modelId }) as string}
      className="model-hub-model-row flex h-9 w-full min-w-0 items-center gap-2.5 overflow-hidden rounded-lg border border-border px-3 text-left"
    >
      <span className="flex min-w-0 flex-1 items-center justify-between gap-2.5">
        <span className="flex min-w-0 flex-1 items-center gap-[7px]"><span className="min-w-0 flex-1 truncate font-mono text-[12px] font-medium text-foreground" title={modelId}>{modelId}</span><span className="model-hub-model-mode-chip shrink-0 rounded-full border border-border px-2 py-[3px] text-[10.5px] font-semibold text-muted">{mode}</span></span>
        <span className={cn('model-hub-model-current min-w-0 flex-1 truncate text-[10.5px]', takeover && 'model-hub-model-current--takeover')} title={currentCopy}>{currentCopy}</span>
      </span>
      <ChevronRight className="model-hub-overview-chevron size-[15px] shrink-0" aria-hidden="true" />
    </button>
  );
};

const AgentModelCard: React.FC<{
  agent: AgentSupply;
  runtime: RuntimeDependency | null;
  sources: Source[];
  chains: ModelChainIndex;
  pending: boolean;
  connecting: boolean;
  switchFailed: boolean;
  onConnectHub: (agent: AgentSupply) => void;
  onSwitchDirect: (agent: AgentSupply) => void;
  onOpenOrder: (agent: AgentSupply) => void;
  onOpenRoute: (agent: AgentSupply, modelId: string) => void;
  onProbeSettled: (agent: AgentSupply) => void;
}> = ({ agent, runtime, sources, chains, pending, connecting, switchFailed, onConnectHub, onSwitchDirect, onOpenOrder, onOpenRoute, onProbeSettled }) => {
  const { t } = useTranslation();
  const { Icon, accent } = backendVisual(agent.backend);
  const [expanded, setExpanded] = React.useState(false);
  const allModels = listedModelIds(agent);
  const chainProjectionLive = agentHasLiveChainProjection(runtime, agent);
  const collapsed = collapsedModelRows(agent, expanded);
  const collapsedAtRest = collapsedModelRows(agent);
  const models = collapsed.visible;
  const canCollapse = collapsedAtRest.hidden.length > 0;
  const needsChainRepair = chainProjectionLive
    && allModels.some((modelId) => {
      const read = chains[modelChainKey(agent.backend, modelId)];
      return read?.kind === 'unread' || read?.kind === 'error';
    });
  const hasTakeover = chainProjectionLive
    && allModels.some((modelId) => isTakeoverRead(chains[modelChainKey(agent.backend, modelId)]));
  const modeWord = t(`settings.models.gateway.group.mode.${agent.mode === 'hub' ? 'gateway' : 'direct'}`) as string;
  const health = agent.supply_status ?? 'noSelection';
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
    && agent.model_supply?.every((entry) => entry.chain_length === 0);
  const statusClass = switchFailed || health === 'interrupted'
    ? 'text-destructive'
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
  return (
    <section className="overflow-hidden rounded-xl border border-border bg-background" data-agent-backend={agent.backend}>
      <div className="flex min-h-[66px] flex-col justify-center gap-[7px] border-b border-border px-3.5 py-3 sm:h-[66px] sm:py-0">
        <div className="flex min-w-0 flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
          <div className="flex min-w-0 items-center gap-[9px]">
            <span className={cn('flex size-[30px] shrink-0 items-center justify-center rounded-[9px]', ACCENT_TILE[accent])}><Icon className={cn('size-[15px]', ACCENT_ICON[accent])} /></span>
            <span className="flex min-w-0 items-center gap-[7px]">
              <h2 className="truncate text-[14px] font-bold text-foreground">{t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend })}</h2>
              <Badge variant="secondary" className={cn('px-2 py-[3px] text-[10.5px] font-semibold', hasTakeover && 'model-hub-takeover-chip')}>
                {hasTakeover ? t('settings.models.takeover.chip') : t('settings.models.gateway.modelCount', { count: allModels.length })}
              </Badge>
            </span>
          </div>
          {agent.mode === 'hub' ? <div className="flex shrink-0 flex-wrap gap-2"><Button variant="secondary" size="sm" className="h-auto rounded-lg px-3 py-[9px] text-[11.5px] font-bold" onClick={() => onOpenOrder(agent)} disabled={pending}>{t('settings.models.gateway.sourceOrder')}</Button><Button variant="outline" size="sm" className="h-auto rounded-lg px-3 py-[9px] text-[11.5px] font-bold" onClick={() => onSwitchDirect(agent)} disabled={pending}>{t(switchFailed ? 'settings.models.gateway.retry' : 'settings.models.gateway.switchToDirect')}</Button></div> : <Button variant="secondary" size="sm" className="h-auto shrink-0 self-start rounded-lg px-3 py-[9px] text-[11.5px] font-bold sm:self-auto" onClick={() => onConnectHub(agent)} disabled={connecting}>{t('settings.models.gateway.switchToGateway')}</Button>}
        </div>
        <span className={cn('flex items-center gap-1.5 text-[11px] font-semibold sm:ml-[42px]', statusClass)}>
          <span className={cn('size-[5px] shrink-0 rounded-full', statusDot)} />
          {switchFailed ? t('settings.models.gateway.fail.switchToDirect') : subtitle}
          <ModelHubInfoHint label={switchFailed ? t('settings.models.gateway.fail.switchToDirect') : subtitle} content={switchFailed ? t('settings.models.gateway.fail.switchToDirect') : subtitle} className="model-hub-overview-info size-[13px]" />
        </span>
      </div>
      {agent.mode === 'hub' && (models.length === 0 ? <div className="px-4 py-10 text-center sm:px-5"><p className="text-[12.5px] text-muted">{t('settings.models.gateway.group.emptyModels')}</p></div> : <div className="space-y-2 p-2">{noUsableSource && <p className="px-3 py-1 text-[11px] font-semibold text-muted">{t('settings.models.gateway.supply.none')}</p>}{models.map((modelId) => <ModelRow key={modelId} agent={agent} modelId={modelId} sources={sources} read={chainProjectionLive ? chains[modelChainKey(agent.backend, modelId)] : undefined} onOpenRoute={onOpenRoute} />)}{canCollapse ? <button type="button" onClick={toggleCollapsed} className="model-hub-model-collapse flex h-6 w-full items-center gap-1.5 hover:text-foreground">{expanded ? <ChevronUp /> : <ChevronDown />}{expanded ? t('settings.models.gateway.collapse', { count: collapsedAtRest.hidden.length }) : t('settings.models.gateway.collapse', { count: collapsed.hidden.length })}</button> : needsChainRepair ? <button type="button" onClick={retryChains} className="model-hub-model-collapse flex h-6 w-full items-center gap-1.5 hover:text-foreground"><RefreshCw />{t('settings.models.gateway.retry')}</button> : null}</div>)}
    </section>
  );
};

export const AgentCard: React.FC<{
  agents: AgentSupply[];
  runtime: RuntimeDependency | null;
  sources: Source[];
  chains: ModelChainIndex;
  pendingBackends: ReadonlySet<string>;
  switchFailures: ReadonlySet<string>;
  onConnectHub: (agent: AgentSupply) => void;
  onSwitchDirect: (agent: AgentSupply) => void;
  onOpenOrder: (agent: AgentSupply) => void;
  onOpenRoute: (agent: AgentSupply, modelId: string) => void;
  onProbeSettled: (agent: AgentSupply) => void;
  connectingBackend: string | null;
}> = ({ agents, pendingBackends, switchFailures, connectingBackend, ...props }) => <div className="flex flex-col gap-4">{agents.map((agent) => <AgentModelCard key={agent.backend} agent={agent} {...props} pending={pendingBackends.has(agent.backend)} switchFailed={switchFailures.has(agent.backend)} connecting={connectingBackend === agent.backend} />)}</div>;
