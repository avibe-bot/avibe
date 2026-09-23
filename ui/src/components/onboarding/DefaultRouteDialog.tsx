import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { ArrowDown, ArrowUp, CircleX, LoaderCircle, Plus, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { DialogOverlay } from '@/components/ui/dialog';
import { useApi } from '@/context/ApiContext';
import type { CollectionReadAuthority } from '@/components/settings/models/collectionReadAuthority';
import { eligibleSources } from '@/components/settings/models/eligibility';
import { modelsApi } from '@/components/settings/models/modelsApi';
import { RouteCandidatePopover } from '@/components/settings/models/RouteCandidatePopover';
import { RouteOriginBadge } from '@/components/settings/models/RouteOriginBadge';
import { routeCandidates, type RouteCandidate } from '@/components/settings/models/routeChainDraft';
import type { AgentSupply, RouteHop, Source } from '@/components/settings/models/types';

import type { SetupFlowState, SetupScreenId } from './setupFlow';
import {
  appendSharedHop,
  hopsFor,
  hydrateSetupRoutes,
  moveRouteHop,
  retrySetupRoutes,
  saveNeedsRetry,
  saveSetupRoutes,
  selectSetupRouteTarget,
  targetKey,
  withMembershipHop,
  type SetupRouteFocus,
  type SetupRouteTargetSnapshot,
  type TargetSaveResult,
} from './setupRoute';

import './onboarding-providers.css';

type DefaultRouteDialogProps = {
  open: boolean;
  onClose: () => void;
  flowState: SetupFlowState;
  setFlowState: React.Dispatch<React.SetStateAction<SetupFlowState>>;
  onNavigate: (screen: SetupScreenId) => void;
  agentReads: CollectionReadAuthority<AgentSupply[]>;
  focus?: SetupRouteFocus | null;
  onSaved?: (focus: SetupRouteFocus) => void | Promise<void>;
};

const hopLabel = (hop: RouteHop, sources: Source[]): string => {
  const source = sources.find((row) => row.id === hop.source_id);
  return source ? `${source.display_name} · ${hop.model_id}` : hop.model_id;
};

export function DefaultRouteDialog({
  open,
  onClose,
  flowState,
  setFlowState,
  onNavigate,
  agentReads,
  focus = null,
  onSaved,
}: DefaultRouteDialogProps) {
  const { t } = useTranslation();
  const api = useApi();
  const [phase, setPhase] = React.useState<'idle' | 'loading' | 'saving' | 'failed'>('idle');
  const [status, setStatus] = React.useState('');
  const [sources, setSources] = React.useState<Source[]>([]);
  const [supplies, setSupplies] = React.useState<AgentSupply[]>([]);
  const [targets, setTargets] = React.useState<SetupRouteTargetSnapshot[]>([]);
  const [receipts, setReceipts] = React.useState<TargetSaveResult[]>([]);
  const baselines = React.useRef<SetupRouteTargetSnapshot[]>([]);
  const addButtonRef = React.useRef<HTMLButtonElement>(null);
  const loadToken = React.useRef(0);
  const dirtyRef = React.useRef(flowState.routeOrderDirty);

  React.useLayoutEffect(() => {
    dirtyRef.current = flowState.routeOrderDirty;
  }, [flowState.routeOrderDirty]);

  const writes = React.useMemo(() => ({
    getVibeAgent: (name: string, params?: { cache?: boolean }) => api.getVibeAgent(name, params),
    listAgents: () => agentReads.readValue(),
    getAgentChain: modelsApi.getAgentChain,
    previewAgentChain: modelsApi.previewAgentChain,
    putAgentChain: modelsApi.putAgentChain,
    getAgentModelCandidates: modelsApi.getAgentModelCandidates,
    putAgentModels: modelsApi.putAgentModels,
  }), [api, agentReads]);

  const load = React.useCallback(async () => {
    const token = ++loadToken.current;
    setPhase('loading');
    setStatus('');
    try {
      const [supplyRead, listed] = await Promise.all([
        agentReads.read(),
        modelsApi.listSources().catch(() => [] as Source[]),
      ]);
      if (token !== loadToken.current) return;
      const nextSupplies = supplyRead.kind === 'current' ? supplyRead.value : [];
      setSources(Array.isArray(listed) ? listed : []);
      setSupplies(nextSupplies);
      const hydration = await hydrateSetupRoutes({
        listVibeAgents: (params) => api.listVibeAgents(params),
        getVibeAgent: (name, params) => api.getVibeAgent(name, params),
        getAgentChain: modelsApi.getAgentChain,
      }, nextSupplies);
      if (token !== loadToken.current) return;
      if (!dirtyRef.current || baselines.current.length === 0) {
        baselines.current = hydration.targets;
        setTargets(hydration.targets);
        setFlowState((current) => (
          current.routeOrderDirty
            ? current
            : { ...current, routeOrder: hydration.union, routeOrderDirty: false }
        ));
      }
      setPhase('idle');
    } catch (error) {
      if (token !== loadToken.current) return;
      setPhase('failed');
      setStatus(error instanceof Error ? error.message : String(error));
    }
  }, [agentReads, api, setFlowState]);

  React.useEffect(() => {
    if (!open) return;
    void load();
  }, [open, load]);

  const close = () => {
    if (phase === 'saving') return;
    onClose();
  };

  const addSource = () => {
    onClose();
    onNavigate('providers');
  };

  const persist = async (retry: boolean) => {
    if (phase === 'saving') return;
    if (!flowState.routeOrderDirty) {
      onClose();
      return;
    }
    setPhase('saving');
    setStatus('');
    try {
      const next = retry
        ? await retrySetupRoutes(flowState.routeOrder, targets, receipts, writes)
        : await saveSetupRoutes(flowState.routeOrder, targets, writes, { dirty: true });
      setReceipts(next);
      if (saveNeedsRetry(next)) {
        setPhase('failed');
        const failed = next.find((row) => row.kind === 'failed' || row.kind === 'reconcile');
        setStatus(failed && failed.kind === 'failed' ? failed.error : t('common.retry'));
        return;
      }
      const confirmed = next.flatMap((row) => {
        if (row.kind !== 'confirmed') return [];
        const live = targets.find((target) => targetKey(target.backend, target.modelId) === row.key);
        if (!live) return [];
        return [{
          ...live,
          chain: row.chain,
          membership: row.chain.manual_override?.hops.length
            ? row.chain.manual_override.hops
            : live.membership,
        }];
      });
      if (confirmed.length) {
        const nextTargets = targets.map((target) =>
          confirmed.find((row) => row.backend === target.backend && row.modelId === target.modelId) ?? target);
        baselines.current = nextTargets;
        setTargets(nextTargets);
      }
      setFlowState((current) => ({ ...current, routeOrderDirty: false }));
      setPhase('idle');
      const selected = selectSetupRouteTarget(targets, focus);
      const savedFocus = focus ?? (selected
        ? { backend: selected.backend, agentName: selected.agentNames[0] }
        : null);
      onClose();
      if (savedFocus) await onSaved?.(savedFocus);
    } catch (error) {
      setPhase('failed');
      setStatus(error instanceof Error ? error.message : String(error));
    }
  };

  const pickerTarget = selectSetupRouteTarget(targets, focus);
  const origin = (pickerTarget?.chain.route_origin === 'manual' || flowState.routeOrderDirty)
    ? 'manual' as const
    : pickerTarget?.chain.route_origin ?? null;

  const rows = flowState.routeOrder;
  const pickerSupply = pickerTarget
    ? supplies.find((row) => row.backend === pickerTarget.backend && row.mode === 'hub') ?? null
    : null;
  const addCandidates = pickerSupply
    ? routeCandidates(pickerSupply, sources, pickerTarget?.membership ?? [])
    : [];
  const targetSources = pickerSupply ? eligibleSources(sources, pickerSupply) : [];
  const canAddHop = Boolean(pickerTarget)
    && (addCandidates.length > 0 || targetSources.some((source) => source.kind === 'api_key'));

  const applyHop = (candidate: RouteCandidate) => {
    if (!pickerTarget || phase === 'saving') return;
    setTargets((current) => current.map((target) => (
      target.backend === pickerTarget.backend && target.modelId === pickerTarget.modelId
        ? { ...target, membership: withMembershipHop(target.membership, candidate.hop) }
        : target
    )));
    setFlowState((current) => ({
      ...current,
      routeOrder: appendSharedHop(current.routeOrder, candidate.hop),
      routeOrderDirty: true,
    }));
  };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={(next) => { if (!next) close(); }}>
      <DialogPrimitive.Portal>
        <DialogOverlay />
        <DialogPrimitive.Content
          className="setup-add-dialog fixed left-1/2 top-1/2 z-50 flex -translate-x-1/2 -translate-y-1/2 flex-col outline-none"
          onEscapeKeyDown={(event) => { if (phase === 'saving') event.preventDefault(); }}
          onPointerDownOutside={(event) => { if (phase === 'saving') event.preventDefault(); }}
        >
          <header className="setup-add-head">
            <div className="flex items-start justify-between gap-3">
              <DialogPrimitive.Title className="setup-add-title">
                {t('onboarding.route.title')}
              </DialogPrimitive.Title>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="setup-add-close size-7 shrink-0"
                aria-label={t('common.close')}
                disabled={phase === 'saving'}
                onClick={close}
              >
                <X className="size-[15px]" />
              </Button>
            </div>
            <DialogPrimitive.Description className="setup-add-subtitle">
              {t('onboarding.route.description')}
            </DialogPrimitive.Description>
            {pickerTarget && (
              <RouteOriginBadge origin={origin} backend={pickerTarget.backend} interactive={false} />
            )}
          </header>

          <div className="setup-add-body">
            {phase === 'loading' && (
              <p className="setup-add-note">{t('common.loading')}</p>
            )}
            {rows.length === 0 && phase !== 'loading' && (
              <p className="setup-add-note">{t('settings.models.routing.draftEmpty')}</p>
            )}
            {rows.length === 1 && (
              <p className="setup-add-note">{t('onboarding.route.singleNote')}</p>
            )}
            {rows.map((hop, index) => {
              const names = hopsFor(targets, hop);
              return (
                <div key={`${hop.source_id}:${hop.model_id}:${index}`} className="setup-add-row">
                  <div className="setup-add-row-copy">
                    <span className="setup-add-row-tag">
                      {index === 0
                        ? t('onboarding.route.preferred')
                        : t('onboarding.route.backup', { index })}
                    </span>
                    <span className="setup-add-row-name">{hopLabel(hop, sources)}</span>
                    {names.length > 0 && (
                      <span className="setup-add-row-detail">{names.join(', ')}</span>
                    )}
                  </div>
                  <div className="flex shrink-0 gap-1">
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="size-7"
                      aria-label={t('onboarding.route.moveUpNamed', { name: hopLabel(hop, sources) })}
                      disabled={phase === 'saving' || index === 0}
                      onClick={() => setFlowState((current) => ({
                        ...current,
                        routeOrder: moveRouteHop(current.routeOrder, index, -1),
                        routeOrderDirty: true,
                      }))}
                    >
                      <ArrowUp className="size-3.5" />
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="size-7"
                      aria-label={t('onboarding.route.moveDownNamed', { name: hopLabel(hop, sources) })}
                      disabled={phase === 'saving' || index === rows.length - 1}
                      onClick={() => setFlowState((current) => ({
                        ...current,
                        routeOrder: moveRouteHop(current.routeOrder, index, 1),
                        routeOrderDirty: true,
                      }))}
                    >
                      <ArrowDown className="size-3.5" />
                    </Button>
                  </div>
                </div>
              );
            })}
            {canAddHop && pickerTarget && (
              <RouteCandidatePopover
                candidates={addCandidates}
                sources={targetSources}
                confirmLabel={t('settings.models.routeDialog.add.confirm')}
                label={t('settings.models.routeDialog.addHop')}
                width="trigger"
                onApply={applyHop}
                onReturnFocus={() => addButtonRef.current?.focus()}
                trigger={
                  <button
                    ref={addButtonRef}
                    type="button"
                    disabled={phase === 'saving' || phase === 'loading'}
                    className="setup-add-row flex w-full items-center justify-center gap-1.5"
                  >
                    <Plus className="size-3.5" aria-hidden="true" />
                    {t('settings.models.routeDialog.addHop')}
                  </button>
                }
              />
            )}
          </div>

          <footer className="setup-add-foot">
            <span className="setup-add-status" role="status" aria-live="polite">
              {phase === 'saving' && (
                <>
                  <LoaderCircle className="setup-add-status-icon motion-safe:animate-spin" aria-hidden="true" />
                  {t('common.loading')}
                </>
              )}
              {phase === 'failed' && (
                <>
                  <CircleX className="setup-add-status-icon setup-add-status-icon--error" aria-hidden="true" />
                  <span className="setup-add-status-error">{status}</span>
                </>
              )}
            </span>
            <Button type="button" variant="outline" size="sm" disabled={phase === 'saving'} onClick={addSource}>
              {t('onboarding.route.addSource')}
            </Button>
            <Button
              type="button"
              variant="brand"
              size="sm"
              disabled={phase === 'loading' || phase === 'saving'}
              onClick={() => void persist(phase === 'failed')}
            >
              {phase === 'failed' ? t('common.retry') : t('onboarding.route.done')}
            </Button>
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
