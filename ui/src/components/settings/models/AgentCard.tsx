import * as React from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowDownUp,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Loader2,
  Plus,
  Settings2,
  TriangleAlert,
  Zap,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { SegmentedRadio } from '@/components/ui/segmented';
import { cn } from '@/lib/utils';
import { apiFailure, modelsApi } from './modelsApi';
import {
  dryRunOutcome,
  probeArrival,
  repairAction,
  REPAIR_LABEL_KEY,
  type DryRunOutcome,
  type RepairKind,
} from './repair';
import { serverText } from './serverCopy';
import { ModelRoutePicker } from './ModelRoutePicker';
import { useAnnounceEnrollment } from './menus/enrollment';
import {
  agentNeedsModelSelection,
  listedModelIds,
  modelChainKey,
  modelNeedsAction,
  type ModelChainIndex,
  type ModelChainRead,
} from './modelRows';
import { ACCENT_ICON, ACCENT_TILE, backendVisual } from './vendorMeta';
import type { RaisedRepair } from './SourceRowMenu';
import type { AgentBackend, AgentChainLink, AgentSupply, RuntimeDependency, Source } from './types';

type ModelTone = 'ok' | 'cooldown' | 'attention' | 'neutral';

const TONE_DOT: Record<ModelTone, string> = {
  ok: 'bg-mint',
  cooldown: 'bg-muted',
  attention: 'bg-destructive',
  neutral: 'bg-muted/60',
};

const sourceName = (sources: Source[], sourceId: string): string =>
  sources.find((source) => source.id === sourceId)?.display_name ?? sourceId;

const runnableHead = (read: ModelChainRead | undefined): AgentChainLink | null =>
  read?.kind === 'ready' ? read.chain.chain.find((link) => link.runnable) ?? null : null;

const blockedRepair = (
  read: ModelChainRead | undefined,
  sources: Source[],
): { source: Source; kind: RepairKind } | null => {
  if (read?.kind !== 'ready') return null;
  for (const link of read.chain.chain) {
    const source = sources.find((candidate) => candidate.id === link.source_id);
    if (!source) continue;
    const kind = repairAction(source);
    if (kind) return { source, kind };
  }
  return null;
};

const isRaisedRepair = (kind: RepairKind): kind is RaisedRepair => kind !== 'retest';

const ModelProbe: React.FC<{
  agent: AgentSupply;
  modelId: string;
  sources: Source[];
  disabled?: boolean;
  onSettled: () => void;
}> = ({ agent, modelId, sources, disabled, onSettled }) => {
  const { t } = useTranslation();
  const [running, setRunning] = React.useState(false);
  const [outcome, setOutcome] = React.useState<DryRunOutcome | null>(null);
  const [errorReason, setErrorReason] = React.useState<{ key: string | null } | null>(null);
  const seq = React.useRef(0);

  React.useEffect(() => {
    seq.current += 1;
    setRunning(false);
    setOutcome(null);
    setErrorReason(null);
  }, [agent.backend, modelId, agent.sources?.policy, agent.sources?.order, agent.mappings]);

  const run = async () => {
    if (agent.mode !== 'hub' || running || disabled) return;
    const mine = ++seq.current;
    setRunning(true);
    setOutcome(null);
    setErrorReason(null);
    try {
      const probe = await modelsApi.probeAgent(agent.backend, modelId);
      const arrival = probeArrival({ kind: 'result', probe }, seq.current === mine);
      if (arrival.report) setOutcome(dryRunOutcome(probe, sources));
      if (arrival.reread) onSettled();
    } catch (error) {
      const failure = apiFailure(error);
      const arrival = probeArrival(
        {
          kind: 'thrown',
          code: failure?.code ?? null,
          serverNamed: failure?.serverNamed ?? false,
        },
        seq.current === mine,
      );
      if (arrival.report) setErrorReason({ key: failure?.detail ?? null });
      if (arrival.reread) onSettled();
    } finally {
      if (seq.current === mine) setRunning(false);
    }
  };

  const result = outcome
    ? outcome.kind === 'ok'
      ? outcome.channel === 'native_cli'
        ? t('settings.models.probe.nativeReady', { source: outcome.sourceName }) as string
        : t('settings.models.probe.hubOk', {
            source: outcome.sourceName,
            ms: outcome.latencyMs,
          }) as string
      : t('settings.models.probe.failed', {
          source: outcome.sourceName,
          detail: serverText(t, outcome.detailKey, 'settings.models.probe.unknown'),
        }) as string
    : errorReason
      ? serverText(t, errorReason.key, 'settings.models.probe.error')
      : null;

  return (
    <div className="flex flex-col items-start gap-2 sm:items-end">
      <Button
        variant="outline"
        size="sm"
        className="h-9"
        onClick={() => void run()}
        disabled={agent.mode !== 'hub' || disabled || running}
      >
        {running ? <Loader2 className="animate-spin" /> : <Zap />}
        {t(running ? 'settings.models.probe.running' : 'settings.models.probe.action')}
      </Button>
      {result && (
        <p className={cn('max-w-sm text-[11.5px] leading-relaxed', outcome?.kind === 'ok' ? 'text-mint' : 'text-gold')}>
          {result}
        </p>
      )}
    </div>
  );
};

const RoutePanel: React.FC<{
  agent: AgentSupply;
  modelId: string;
  sources: Source[];
  read: ModelChainRead | undefined;
  pending: boolean;
  onSetRoute: (backend: AgentBackend, modelId: string, targetModelId: string | null) => void;
  onAddModel: () => void;
  onProbeSettled: () => void;
}> = ({ agent, modelId, sources, read, pending, onSetRoute, onAddModel, onProbeSettled }) => {
  const { t } = useTranslation();
  const stored = agent.mappings?.find((mapping) => mapping.builtin_id === modelId && mapping.enabled);
  const storedTarget = stored?.target_model_id ?? null;
  const [choice, setChoice] = React.useState<'global' | 'manual'>(stored ? 'manual' : 'global');
  React.useEffect(() => setChoice(storedTarget ? 'manual' : 'global'), [storedTarget]);

  const head = runnableHead(read);
  const actualSource = head ? sourceName(sources, head.source_id) : null;
  const actualTarget = head?.resolved_model_id && head.resolved_model_id !== modelId ? head.resolved_model_id : null;

  return (
    <div className="grid gap-4 border-t border-border bg-foreground/[0.015] px-4 py-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:px-5">
      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-[12px] font-semibold text-foreground">{t('settings.models.routes.title')}</span>
          {actualSource && (
            <span className="truncate text-[11.5px] text-muted">
              {t('settings.models.routes.liveSource', {
                source: actualSource,
                model: actualTarget ?? modelId,
              })}
            </span>
          )}
        </div>

        {agent.menu_kind === 'fixed' ? (
          <>
            <SegmentedRadio
              value={choice}
              onChange={(next) => {
                if (next === 'global' && stored) onSetRoute(agent.backend, modelId, null);
                else setChoice(next);
              }}
              options={[
                { id: 'global', label: t('settings.models.routes.global') as string },
                { id: 'manual', label: t('settings.models.routes.manual') as string },
              ]}
              ariaLabel={t('settings.models.routes.title') as string}
              disabled={pending}
            />
            {choice === 'global' ? (
              <p className="text-[11.5px] leading-relaxed text-muted">
                {stored
                  ? t('settings.models.routes.globalPending')
                  : actualSource
                    ? t('settings.models.routes.globalActual', { source: actualSource })
                    : t('settings.models.routes.globalUnavailable')}
              </p>
            ) : (
              <div className="space-y-2">
                <ModelRoutePicker
                  agent={agent}
                  sources={sources}
                  value={stored?.target_model_id ?? ''}
                  servedBy={actualSource}
                  disabled={pending}
                  onChange={(targetModelId) => onSetRoute(agent.backend, modelId, targetModelId)}
                  onAddModel={onAddModel}
                />
                <p className="flex items-start gap-1.5 text-[11px] leading-relaxed text-gold">
                  <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
                  {t('settings.models.routes.compatibility')}
                </p>
              </div>
            )}
          </>
        ) : (
          <p className="rounded-lg border border-border bg-background px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
            {t('settings.models.routes.openMenuGlobal', { source: actualSource ?? t('settings.models.routes.none') })}
          </p>
        )}
      </div>

      <ModelProbe
        agent={agent}
        modelId={modelId}
        sources={sources}
        disabled={pending}
        onSettled={onProbeSettled}
      />
    </div>
  );
};

const ModelRow: React.FC<{
  agent: AgentSupply;
  modelId: string;
  sources: Source[];
  read: ModelChainRead | undefined;
  runtime: RuntimeDependency | null;
  pending: boolean;
  onSetRoute: (backend: AgentBackend, modelId: string, targetModelId: string | null) => void;
  onAddModel: () => void;
  onRepair: (source: Source, kind: RaisedRepair) => void;
  onRetest: (source: Source) => void;
  retestingSourceId: string | null;
  onProbeSettled: () => void;
}> = ({
  agent,
  modelId,
  sources,
  read,
  runtime,
  pending,
  onSetRoute,
  onAddModel,
  onRepair,
  onRetest,
  retestingSourceId,
  onProbeSettled,
}) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);
  const configurable = agent.mode === 'hub';
  const current = agent.selected_model_id === modelId;
  const head = runnableHead(read);
  const headIndex = read?.kind === 'ready' && head ? read.chain.chain.indexOf(head) : -1;
  const runtimeBlocked = head?.channel === 'hub' && Boolean(runtime && runtime.status.health !== 'ok');
  const needsAction = modelNeedsAction(agent, modelId, read, runtime);
  const repair = blockedRepair(read, sources);
  const raisedRepair = repair && isRaisedRepair(repair.kind)
    ? { source: repair.source, kind: repair.kind }
    : null;
  const structuralEmpty =
    read?.kind === 'ready'
      ? read.chain.chain.length === 0
      : agent.model_supply?.find((model) => model.model_id === modelId)?.chain_length === 0;

  let tone: ModelTone = 'neutral';
  let status = t('settings.models.modelStatus.checking') as string;
  let detail = '';
  if (agent.mode === 'direct') {
    status = t('settings.models.modelStatus.direct') as string;
  } else if (read?.kind === 'ready' && read.chain.supply_state === 'waiting') {
    tone = 'cooldown';
    status = t('settings.models.modelStatus.cooldown') as string;
    const waiting = read.chain.chain[0];
    if (waiting) detail = sourceName(sources, waiting.source_id);
  } else if (runtimeBlocked) {
    tone = 'attention';
    status = t('settings.models.modelStatus.needsAction') as string;
    detail = t('settings.models.modelStatus.runtime') as string;
  } else if (needsAction) {
    tone = 'attention';
    status = t('settings.models.modelStatus.needsAction') as string;
    detail = read?.kind === 'error'
      ? t('settings.models.modelStatus.unknown') as string
      : structuralEmpty
        ? t('settings.models.modelStatus.noSource') as string
        : t('settings.models.modelStatus.blocked') as string;
  } else if (head) {
    tone = 'ok';
    status = t('settings.models.modelStatus.ok') as string;
    const serving = sourceName(sources, head.source_id);
    detail = t(
      current
        ? headIndex > 0
          ? 'settings.models.modelStatus.currentSwitched'
          : 'settings.models.modelStatus.currentSource'
        : 'settings.models.modelStatus.source',
      { source: serving },
    ) as string;
  } else if (read?.kind === 'error') {
    status = t('settings.models.modelStatus.unknown') as string;
  }

  const action = needsAction ? (
    read?.kind === 'error' ? (
      <Button variant="outline" size="xs" className="h-7 shrink-0" onClick={onProbeSettled}>
        {t('settings.models.modelStatus.retry')}
      </Button>
    ) : runtimeBlocked ? (
      <Button asChild variant="outline" size="xs" className="h-7 shrink-0">
        <Link to="/admin/settings/dependencies">{t('settings.models.modelStatus.runtimeAction')}</Link>
      </Button>
    ) : repair?.kind === 'retest' ? (
      <Button
        variant="outline"
        size="xs"
        className="h-7 shrink-0"
        onClick={() => onRetest(repair.source)}
        disabled={retestingSourceId !== null}
      >
        {retestingSourceId === repair.source.id && <Loader2 className="animate-spin" />}
        {t(REPAIR_LABEL_KEY.retest)}
      </Button>
    ) : raisedRepair ? (
      <Button
        variant="outline"
        size="xs"
        className="h-7 shrink-0"
        onClick={() => onRepair(raisedRepair.source, raisedRepair.kind)}
      >
        {t(REPAIR_LABEL_KEY[raisedRepair.kind])}
      </Button>
    ) : agent.menu_kind === 'open' ? (
      <Button variant="outline" size="xs" className="h-7 shrink-0" onClick={onAddModel}>
        <Plus />
        {t('settings.models.sources.addModel')}
      </Button>
    ) : (
      <Button variant="outline" size="xs" className="h-7 shrink-0" onClick={() => setExpanded(true)}>
        {t('settings.models.routes.manual')}
      </Button>
    )
  ) : null;

  return (
    <div className={cn('border-b border-border last:border-b-0', current && 'bg-mint-soft/35')} data-model-issue={needsAction || undefined}>
      <div className="group grid min-w-0 grid-cols-[minmax(0,1fr)] items-center gap-3 px-4 py-3 sm:grid-cols-[minmax(180px,0.9fr)_minmax(260px,1.4fr)_auto] sm:px-5">
        <button
          type="button"
          onClick={() => configurable && setExpanded((value) => !value)}
          className="min-w-0 text-left"
        >
          <span className="flex min-w-0 items-center gap-2">
            <span className="truncate font-mono text-[12.5px] font-semibold text-foreground">{modelId}</span>
            {current && (
              <Badge variant="success" className="shrink-0 px-1.5 py-0 text-[9.5px]">
                {t('settings.models.current')}
              </Badge>
            )}
          </span>
        </button>

        <button
          type="button"
          onClick={() => configurable && setExpanded((value) => !value)}
          className="col-start-1 row-start-2 flex min-w-0 items-center gap-2 text-left sm:col-start-2 sm:row-start-1"
        >
          <span className={cn('size-2 shrink-0 rounded-full', TONE_DOT[tone])} aria-hidden />
          <span className={cn('shrink-0 text-[11.5px] font-semibold', tone === 'attention' ? 'text-destructive' : tone === 'ok' ? 'text-mint' : 'text-muted')}>
            {status}
          </span>
          {detail && <span className="min-w-0 truncate text-[11.5px] text-muted">· {detail}</span>}
        </button>

        <div className="col-start-1 row-start-3 flex min-w-0 items-center justify-end gap-1.5 sm:col-start-3 sm:row-start-1">
          {action}
          {configurable && (
            <button
              type="button"
              aria-label={t('settings.models.routes.expand') as string}
              onClick={() => setExpanded((value) => !value)}
              className={cn(
                'hidden size-8 items-center justify-center rounded-md text-muted transition hover:bg-surface-2 hover:text-foreground sm:flex',
                expanded ? 'opacity-100' : 'opacity-60 sm:opacity-0 sm:group-hover:opacity-100 sm:focus-visible:opacity-100',
              )}
            >
              {expanded ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
            </button>
          )}
        </div>
      </div>
      {configurable && expanded && (
        <RoutePanel
          agent={agent}
          modelId={modelId}
          sources={sources}
          read={read}
          pending={pending}
          onSetRoute={onSetRoute}
          onAddModel={onAddModel}
          onProbeSettled={onProbeSettled}
        />
      )}
    </div>
  );
};

const EmptySelectionRow: React.FC<{
  agent: AgentSupply;
  pending: boolean;
  onOpenModels: (agent: AgentSupply) => void;
}> = ({
  agent,
  pending,
  onOpenModels,
}) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-3 border-b border-destructive/25 bg-destructive/[0.035] px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-5" data-model-issue>
      <span className="flex min-w-0 items-start gap-2.5">
        <CircleAlert className="mt-0.5 size-4 shrink-0 text-destructive" />
        <span>
          <span className="block text-[12.5px] font-semibold text-foreground">{t('settings.models.emptySelection.title')}</span>
          <span className="mt-0.5 block text-[11.5px] text-muted">{t('settings.models.emptySelection.body')}</span>
        </span>
      </span>
      {agent.menu_kind === 'open' ? (
        <Button
          variant="outline"
          size="sm"
          className="h-9 shrink-0"
          onClick={() => onOpenModels(agent)}
          disabled={pending}
        >
          {t('settings.models.emptySelection.action')}
        </Button>
      ) : (
        <Button asChild variant="outline" size="sm" className="h-9 shrink-0">
          <Link to="/agents">{t('settings.models.emptySelection.action')}</Link>
        </Button>
      )}
    </div>
  );
};

const AgentModelCard: React.FC<{
  agent: AgentSupply;
  sources: Source[];
  chains: ModelChainIndex;
  runtime: RuntimeDependency | null;
  issuesOnly: boolean;
  pending: boolean;
  connecting: boolean;
  onConnectHub: (agent: AgentSupply) => void;
  onOpenOrder: (agent: AgentSupply) => void;
  onOpenModels: (agent: AgentSupply) => void;
  onSetRoute: (
    backend: AgentBackend,
    modelId: string,
    targetModelId: string | null,
    onCommitted: (before: AgentSupply, after: AgentSupply) => void,
  ) => void;
  onAddModel: () => void;
  onRepair: (source: Source, kind: RaisedRepair) => void;
  onRetest: (source: Source) => void;
  retestingSourceId: string | null;
  onProbeSettled: () => void;
}> = (props) => {
  const { t } = useTranslation();
  const { agent, sources, chains, runtime, issuesOnly } = props;
  const announceEnrollment = useAnnounceEnrollment(agent.backend, sources);
  const { Icon, accent } = backendVisual(agent.backend);
  const allModels = listedModelIds(agent);
  const models = issuesOnly
    ? allModels.filter((modelId) => modelNeedsAction(agent, modelId, chains[modelChainKey(agent.backend, modelId)], runtime))
    : allModels;
  const emptySelection = agentNeedsModelSelection(agent);
  if (issuesOnly && !emptySelection && models.length === 0) return null;

  return (
    <section className="overflow-hidden rounded-xl border border-border bg-background">
      <div className="flex flex-col gap-3 border-b border-border px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-5">
        <div className="flex min-w-0 items-center gap-3">
          <span className={cn('flex size-9 shrink-0 items-center justify-center rounded-[10px]', ACCENT_TILE[accent])}>
            <Icon className={cn('size-[18px]', ACCENT_ICON[accent])} />
          </span>
          <span className="min-w-0">
            <span className="flex items-center gap-2">
              <h2 className="truncate text-[14px] font-bold text-foreground">
                {t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend })}
              </h2>
              {agent.mode === 'direct' && (
                <Badge variant="secondary" className="px-2 py-0 text-[10px]">
                  {t('settings.models.modelStatus.direct')}
                </Badge>
              )}
            </span>
            <span className="mt-0.5 block text-[11.5px] text-muted">
              {t('settings.models.agents.modelListCount', { count: allModels.length })}
            </span>
          </span>
        </div>

        {agent.mode === 'hub' ? (
          <Button
            variant="secondary"
            size="sm"
            className="h-9 shrink-0"
            onClick={() => props.onOpenOrder(agent)}
            disabled={props.pending}
          >
            <ArrowDownUp />
            {t('settings.models.agents.sourceOrder')}
          </Button>
        ) : (
          <Button
            variant="secondary"
            size="sm"
            className="h-9 shrink-0"
            onClick={() => props.onConnectHub(agent)}
            disabled={props.connecting}
          >
            <Settings2 />
            {t('settings.models.agents.enableManaged')}
          </Button>
        )}
      </div>

      {emptySelection && (
        <EmptySelectionRow agent={agent} pending={props.pending} onOpenModels={props.onOpenModels} />
      )}

      {models.length === 0 && !emptySelection ? (
        <div className="flex flex-col items-center gap-3 px-4 py-10 text-center sm:px-5">
          <p className="text-[12.5px] text-muted">{t('settings.models.agents.emptyModels')}</p>
          {agent.menu_kind === 'open' ? (
            <Button
              variant="outline"
              size="sm"
              className="h-9"
              onClick={() => props.onOpenModels(agent)}
              disabled={props.pending}
            >
              <Plus />
              {t('settings.models.emptySelection.action')}
            </Button>
          ) : (
            <Button asChild variant="outline" size="sm" className="h-9">
              <Link to="/agents">
                <Plus />
                {t('settings.models.emptySelection.action')}
              </Link>
            </Button>
          )}
        </div>
      ) : (
        models.map((modelId) => (
          <ModelRow
            key={modelId}
            agent={agent}
            modelId={modelId}
            sources={sources}
            read={chains[modelChainKey(agent.backend, modelId)]}
            runtime={runtime}
            pending={props.pending}
            onSetRoute={(backend, modelId, targetModelId) =>
              props.onSetRoute(backend, modelId, targetModelId, announceEnrollment)
            }
            onAddModel={props.onAddModel}
            onRepair={props.onRepair}
            onRetest={props.onRetest}
            retestingSourceId={props.retestingSourceId}
            onProbeSettled={props.onProbeSettled}
          />
        ))
      )}

      {agent.menu_kind === 'open' && !issuesOnly && (
        <button
          type="button"
          onClick={() => props.onOpenModels(agent)}
          disabled={props.pending}
          className="flex min-h-11 w-full items-center justify-center gap-2 border-t border-border px-4 py-2.5 text-[12px] font-semibold text-muted transition hover:bg-surface-2 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent disabled:hover:text-muted"
        >
          <Settings2 className="size-3.5" />
          {t('settings.models.agents.manageModels')}
        </button>
      )}
    </section>
  );
};

export const AgentCard: React.FC<{
  agents: AgentSupply[];
  sources: Source[];
  chains: ModelChainIndex;
  runtime: RuntimeDependency | null;
  issuesOnly: boolean;
  pendingBackends: ReadonlySet<string>;
  onConnectHub: (agent: AgentSupply) => void;
  onOpenOrder: (agent: AgentSupply) => void;
  onOpenModels: (agent: AgentSupply) => void;
  onSetRoute: (
    backend: AgentBackend,
    modelId: string,
    targetModelId: string | null,
    onCommitted: (before: AgentSupply, after: AgentSupply) => void,
  ) => void;
  onAddModel: () => void;
  onRepair: (source: Source, kind: RaisedRepair) => void;
  onRetest: (source: Source) => void;
  retestingSourceId: string | null;
  onProbeSettled: () => void;
  connectingBackend: string | null;
}> = ({ agents, ...props }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-4">
      {agents.map((agent) => (
        <AgentModelCard
          key={agent.backend}
          agent={agent}
          {...props}
          pending={props.pendingBackends.has(agent.backend)}
          connecting={props.connectingBackend === agent.backend}
        />
      ))}
      {props.issuesOnly && !agents.some((agent) => {
        if (agentNeedsModelSelection(agent)) return true;
        return listedModelIds(agent).some((modelId) =>
          modelNeedsAction(agent, modelId, props.chains[modelChainKey(agent.backend, modelId)], props.runtime),
        );
      }) && (
        <div className="rounded-xl border border-border bg-background px-4 py-10 text-center text-[12.5px] text-muted">
          {t('settings.models.status.noIssues')}
        </div>
      )}
    </div>
  );
};
