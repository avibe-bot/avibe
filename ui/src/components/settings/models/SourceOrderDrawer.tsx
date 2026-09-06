import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Reorder, useDragControls } from 'framer-motion';
import { ArrowUp, ArrowDown, GripVertical, LoaderCircle, Minus, Plus, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import { PROTOCOL_COPY_KEYS } from './addApiKeyState';
import type { PendingWrite } from './asyncLifetime';
import type { CollectionReadAuthority } from './collectionReadAuthority';
import { apiFailure, modelsApi, type GuardConfirmation } from './modelsApi';
import { GuardGapList } from './GuardGapList';
import { mayHaveWritten } from './repair';
import { movedOrder, sameIds } from './reorder';
import { combineSourceOrderReads } from './sourceOrderComposition';
import type { AgentSupply, Source } from './types';

type OrderAnnouncement =
  | { key: 'grabbed' | 'moved' | 'dropped'; source: string; position: number; count: number }
  | { key: 'grabCancelled'; source: string }
  | null;

type ReadState = 'loading' | 'reconciling' | 'ready' | 'error';

const SourceIdentity: React.FC<{ source: Source }> = ({ source }) => {
  const { t } = useTranslation();
  const inventory = source.models.length
    ? t('settings.models.sources.modelCount', { count: source.models.length })
    : t('settings.models.routing.inventoryNotProvided');
  const detail = `${t(PROTOCOL_COPY_KEYS[source.protocol])} · ${inventory}`;
  return (
    <>
      <span className="model-hub-order-identity">
        <span className="model-hub-order-name" title={source.display_name}>{source.display_name}</span>
        {detail && <span className="model-hub-order-meta" title={detail}>{detail}</span>}
      </span>
    </>
  );
};

const OrderedRow: React.FC<{
  source: Source;
  index: number;
  count: number;
  busy: boolean;
  grabbed: boolean;
  handleRef: (node: HTMLButtonElement | null) => void;
  onExclude: () => void;
  onMove: (direction: -1 | 1) => void;
  onKeyDown: (event: React.KeyboardEvent<HTMLButtonElement>) => void;
}> = ({ source, index, count, busy, grabbed, handleRef, onExclude, onMove, onKeyDown }) => {
  const { t } = useTranslation();
  const controls = useDragControls();
  const inert = count <= 1;
  return (
    <Reorder.Item
      value={source.id}
      dragListener={false}
      dragControls={controls}
      className="model-hub-order-row model-hub-order-row--ordered list-none"
    >
      <button
        ref={handleRef}
        type="button"
        aria-label={t('settings.models.order.reorder')}
        aria-grabbed={grabbed}
        disabled={busy || inert}
        onPointerDown={(event) => !inert && controls.start(event)}
        onKeyDown={onKeyDown}
        className={cn('model-hub-order-grip cursor-grab active:cursor-grabbing disabled:cursor-not-allowed', grabbed && 'is-grabbed')}
      >
        <GripVertical />
      </button>
      <span className={cn('model-hub-order-ordinal', index === 0 && 'is-first')}>{index + 1}</span>
      <SourceIdentity source={source} />
      <span className="model-hub-order-row-actions">
        <Button type="button" variant="ghost" size="icon" className="model-hub-route-action model-hub-order-row-action" aria-label={t('settings.models.routing.moveUp')} disabled={busy || index === 0} onClick={() => onMove(-1)}><ArrowUp aria-hidden /></Button>
        <Button type="button" variant="ghost" size="icon" className="model-hub-route-action model-hub-order-row-action" aria-label={t('settings.models.routing.moveDown')} disabled={busy || index === count - 1} onClick={() => onMove(1)}><ArrowDown aria-hidden /></Button>
        <Button type="button" variant="ghost" size="icon" className="model-hub-route-action model-hub-order-row-action" aria-label={t('settings.models.order.action.exclude')} disabled={busy} onClick={onExclude}>
          <Minus aria-hidden />
        </Button>
      </span>
    </Reorder.Item>
  );
};

export const SourceOrderDrawer: React.FC<{
  open: boolean;
  agent: AgentSupply;
  sources: Source[];
  onClose: () => void;
  onSaved: (echoed: AgentSupply) => void | Promise<void>;
  orderWrite: PendingWrite;
  sourceReads: CollectionReadAuthority<Source[]>;
}> = ({ open, agent, sources, onClose, onSaved, orderWrite, sourceReads }) => {
  const { t } = useTranslation();
  const [viewAgent, setViewAgent] = React.useState(agent);
  const [viewSources, setViewSources] = React.useState(sources);
  const [readState, setReadState] = React.useState<ReadState>('loading');
  const [order, setOrder] = React.useState<string[]>([]);
  const [saveFailed, setSaveFailed] = React.useState(false);
  const [guard, setGuard] = React.useState<GuardConfirmation | null>(null);
  const [unknownWrite, setUnknownWrite] = React.useState(false);
  const [grabbedId, setGrabbedId] = React.useState<string | null>(null);
  const [announcement, setAnnouncement] = React.useState<OrderAnnouncement>(null);
  const [saved, setSaved] = React.useState<string[]>([]);
  const grabbedFrom = React.useRef<string[]>([]);
  const handles = React.useRef(new Map<string, HTMLButtonElement>());
  const heldOutActions = React.useRef(new Map<string, HTMLButtonElement>());
  const readAttempt = React.useRef(0);
  const saving = orderWrite.pending;

  const applyRead = React.useCallback((next: AgentSupply, nextSources: Source[], state: ReadState) => {
    const nextOrder = next.sources?.order ?? [];
    setViewAgent(next);
    setViewSources(nextSources);
    setSaved(nextOrder);
    setOrder(nextOrder);
    setSaveFailed(false);
    setGuard(null);
    setUnknownWrite(false);
    setGrabbedId(null);
    setAnnouncement(null);
    setReadState(state);
  }, []);

  const read = React.useCallback(async () => {
    const seq = ++readAttempt.current;
    setReadState('loading');
    try {
      const readPair = () => Promise.all([
        modelsApi.getAgentSources(agent.backend),
        sourceReads.readValue(),
      ] as const);
      const [next, nextSources] = await readPair();
      if (readAttempt.current !== seq) return;
      const composition = combineSourceOrderReads(next, nextSources);
      if (!composition.hasHole) {
        applyRead(next, nextSources, 'ready');
        return;
      }

      // A composition hole is evidence that the two reads straddled a mutation,
      // not that either endpoint failed. Keep it visible while one regroup read runs.
      applyRead(next, nextSources, 'reconciling');
      const [regroupedAgent, regroupedSources] = await readPair();
      if (readAttempt.current !== seq) return;
      const regrouped = combineSourceOrderReads(regroupedAgent, regroupedSources);
      applyRead(regroupedAgent, regroupedSources, regrouped.hasHole ? 'error' : 'ready');
    } catch {
      if (readAttempt.current === seq) setReadState('error');
    }
  }, [agent.backend, applyRead, sourceReads]);

  React.useEffect(() => {
    if (open) void read();
    else readAttempt.current += 1;
  }, [open, read]);

  const composition = combineSourceOrderReads(viewAgent, viewSources);
  const available = composition.available;
  const byId = React.useMemo(() => new Map(available.map((source) => [source.id, source])), [available]);
  const orderedEntries = order.map((id) => ({ id, source: byId.get(id) }));
  const ordered = order.map((id) => byId.get(id)).filter((source): source is Source => Boolean(source));
  const heldOut = available.filter((source) => !order.includes(source.id));
  const persist = (next: string[]) => {
    if (sameIds(next, order)) return;
    setOrder(next);
    setSaveFailed(false);
  };
  const focusHandle = (sourceId: string) => handles.current.get(sourceId)?.focus();
  const focusHandleAfterRender = (sourceId: string) => requestAnimationFrame(() => focusHandle(sourceId));
  const focusHeldOutAfterRender = (sourceId: string) => requestAnimationFrame(() => heldOutActions.current.get(sourceId)?.focus());
  const startGrab = (sourceId: string) => {
    grabbedFrom.current = [...order];
    setGrabbedId(sourceId);
    setAnnouncement({
      key: 'grabbed',
      source: byId.get(sourceId)?.display_name ?? sourceId,
      position: order.indexOf(sourceId) + 1,
      count: order.length,
    });
  };
  const dropGrab = () => {
    if (!grabbedId) return;
    setAnnouncement({
      key: 'dropped',
      source: byId.get(grabbedId)?.display_name ?? grabbedId,
      position: order.indexOf(grabbedId) + 1,
      count: order.length,
    });
    setGrabbedId(null);
  };
  const cancelGrab = () => {
    if (!grabbedId) return;
    const sourceId = grabbedId;
    const restored = grabbedFrom.current;
    setOrder(restored);
    setAnnouncement({ key: 'grabCancelled', source: byId.get(sourceId)?.display_name ?? sourceId });
    setGrabbedId(null);
    requestAnimationFrame(() => focusHandle(sourceId));
  };
  const handleRowKey = (sourceId: string, event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === ' ') {
      event.preventDefault();
      if (grabbedId === sourceId) dropGrab();
      else startGrab(sourceId);
      return;
    }
    if (event.key === 'Escape' && grabbedId === sourceId) {
      event.preventDefault();
      event.stopPropagation();
      cancelGrab();
      return;
    }
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    event.preventDefault();
    const index = order.indexOf(sourceId);
    const delta = event.key === 'ArrowUp' ? -1 : 1;
    if (grabbedId === sourceId) {
      const next = movedOrder(order, index, delta);
      if (sameIds(next, order)) return;
      persist(next);
      setAnnouncement({
        key: 'moved',
        source: byId.get(sourceId)?.display_name ?? sourceId,
        position: next.indexOf(sourceId) + 1,
        count: next.length,
      });
      requestAnimationFrame(() => focusHandle(sourceId));
    } else {
      const target = order[index + delta];
      if (target) focusHandle(target);
    }
  };

  const save = (confirmation?: GuardConfirmation) => {
    if (saving || readState !== 'ready') return;
    void orderWrite.track(async () => {
      try {
        if (unknownWrite) {
          const observed = await modelsApi.getAgentSources(agent.backend);
          if (sameIds(observed.sources?.order ?? [], order)) {
            await onSaved(observed);
            onClose();
            return;
          }
          setUnknownWrite(false);
          setSaveFailed(true);
          return;
        }
        const echoed = await modelsApi.putAgentSources(agent.backend, { order, ...confirmation });
        const nextOrder = echoed.sources?.order ?? order;
        setSaved(nextOrder);
        setOrder(nextOrder);
        setSaveFailed(false);
        await Promise.resolve(onSaved(echoed)).catch(() => {});
        onClose();
      } catch (error) {
        const failure = apiFailure(error);
        if (failure && (failure.wouldRemoveHops.length || failure.wouldInterrupt.length)) {
          setGuard({ force: true, would_remove_hops: failure.wouldRemoveHops, would_interrupt: failure.wouldInterrupt });
          return;
        }
        setUnknownWrite(mayHaveWritten(failure));
        // F1: the request failed, not the user's draft. Keep every move and let
        // the same primary retry the exact order.
        setSaveFailed(true);
      }
    });
  };

  const backend = t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend });
  const title = t('settings.models.order.title', { backend });
  const announcementText = announcement ? t(`settings.models.order.${announcement.key}`, announcement) : '';
  const saveEnabled = readState === 'ready' && (!sameIds(saved, order) || saveFailed);
  const manualCount = (viewAgent.model_supply ?? []).filter((model) => Object.hasOwn(viewAgent.routes ?? {}, model.model_id)).length;
  const inheritedCount = (viewAgent.model_supply?.length ?? 0) - manualCount;

  return (
    <DialogPrimitive.Root open={open} onOpenChange={(next) => !next && !saving && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="model-hub-order-overlay fixed inset-0 z-50" />
        <DialogPrimitive.Content
          className="model-hub-order-drawer fixed inset-y-0 right-0 z-50 flex flex-col overflow-hidden bg-surface outline-none"
          onEscapeKeyDown={(event) => {
            if (guard) {
              event.preventDefault();
              setGuard(null);
            } else if (grabbedId) {
              event.preventDefault();
              cancelGrab();
            } else if (saving) event.preventDefault();
          }}
        >
          <header className="model-hub-order-head shrink-0 border-b border-border">
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-[7px]">
                <DialogPrimitive.Title className="model-hub-order-title truncate font-bold text-foreground">{title}</DialogPrimitive.Title>
                <ModelHubInfoHint
                  label={t('settings.models.order.infoLabel')}
                  content={t('settings.models.order.subtitle')}
                  className="model-hub-order-info"
                />
              </div>
              <DialogPrimitive.Close asChild>
                <Button type="button" variant="ghost" size="icon" className="model-hub-order-close size-[27px]" aria-label={t('settings.models.order.cancel')} disabled={saving}>
                  <X className="size-[15px]" />
                </Button>
              </DialogPrimitive.Close>
            </div>
            <DialogPrimitive.Description className="model-hub-order-subtitle">
              {t('settings.models.order.subtitle')}
            </DialogPrimitive.Description>
          </header>

          <div className="model-hub-order-body flex min-h-0 flex-1 flex-col overflow-y-auto">
            {guard ? <div className="model-hub-order-section" role="alert">
              <h3>{t('settings.models.routing.guardTitle')}</h3>
              <ul className="model-hub-guard-list">{guard.would_remove_hops?.map((hop) => <li key={`${hop.backend}:${hop.menu_model}:${hop.position}`} className="model-hub-guard-hop"><span>{hop.menu_model} · {hop.model_id} · {hop.source_id}</span></li>)}</ul>
              <GuardGapList gaps={guard.would_interrupt ?? []} />
            </div> : <>
            {readState === 'loading' && (
              <div className="model-hub-order-state"><LoaderCircle className="model-hub-ink-mint size-4 animate-spin" />{t('common.loading')}</div>
            )}
            {readState === 'reconciling' && (
              <section className="model-hub-order-section" aria-busy="true">
                <div className="model-hub-order-section-head">
                  <h3>{t('settings.models.order.section.ordered')}</h3>
                  <LoaderCircle className="model-hub-ink-mint size-3.5 animate-spin" aria-label={t('common.loading')} />
                </div>
                <div className="flex flex-col gap-2">
                  {orderedEntries.map(({ id, source: orderedSource }, index) => (
                    <div key={id} className="model-hub-order-row model-hub-order-row--ordered">
                      {orderedSource
                        ? <span className="model-hub-order-grip" />
                        : <LoaderCircle className="model-hub-order-grip animate-spin" aria-hidden />}
                      <span className={cn('model-hub-order-ordinal', index === 0 && 'is-first')}>{index + 1}</span>
                      {orderedSource
                        ? <SourceIdentity source={orderedSource} />
                        : <span className="model-hub-order-identity">
                            <span className="model-hub-order-name font-mono" title={id}>{id}</span>
                          </span>}
                    </div>
                  ))}
                </div>
              </section>
            )}
            {readState === 'error' && (
              <div className="model-hub-order-state model-hub-order-state--error">{t('settings.models.order.fail.read')}</div>
            )}
            {readState === 'ready' && available.length === 0 && (
              <div className="model-hub-order-state">{t('settings.models.order.empty.noEligible')}</div>
            )}
            {readState === 'ready' && available.length > 0 && (
              <>
                <section className="model-hub-order-section">
                  <div className="model-hub-order-section-head">
                    <h3>{t('settings.models.order.section.ordered')}</h3>
                    <span className="model-hub-order-section-explanation">{t('settings.models.order.section.ordered.note')}</span>
                  </div>
                  {ordered.length === 0
                    ? <div className="model-hub-order-empty">{t('settings.models.order.empty.ordered')}</div>
                    : <Reorder.Group axis="y" values={order} onReorder={persist} className="flex flex-col gap-2">
                      {ordered.map((source, index) => (
                        <OrderedRow
                          key={source.id}
                          source={source}
                          index={index}
                          count={ordered.length}
                          onMove={(direction) => { persist(movedOrder(order, index, direction)); focusHandleAfterRender(source.id); }}
                          busy={saving}
                          grabbed={grabbedId === source.id}
                          handleRef={(node) => {
                            if (node) handles.current.set(source.id, node);
                            else handles.current.delete(source.id);
                          }}
                          onExclude={() => {
                            if (grabbedId === source.id) setGrabbedId(null);
                            persist(order.filter((id) => id !== source.id));
                            focusHeldOutAfterRender(source.id);
                          }}
                          onKeyDown={(event) => handleRowKey(source.id, event)}
                        />
                      ))}
                    </Reorder.Group>}
                </section>
                <section className="model-hub-order-section">
                  <div className="model-hub-order-section-head">
                    <h3>{t('settings.models.order.section.heldOut')}</h3>
                    <span className="model-hub-order-section-explanation">{t('settings.models.order.section.heldOut.note')}</span>
                  </div>
                  <div className="flex flex-col gap-2">
                    {heldOut.map((source) => (
                      <div key={source.id} className="model-hub-order-row model-hub-order-row--held">
                        <Minus className="model-hub-order-held-icon" />
                        <SourceIdentity source={source} />
                        <span className="model-hub-order-row-actions">
                          <Button
                            ref={(node) => {
                              if (node) heldOutActions.current.set(source.id, node);
                              else heldOutActions.current.delete(source.id);
                            }}
                            type="button"
                            variant="ghost"
                            size="icon"
                            aria-label={t('settings.models.order.action.include')}
                            className="model-hub-route-action model-hub-order-row-action"
                            disabled={saving}
                            onClick={() => {
                              persist([...order, source.id]);
                              focusHandleAfterRender(source.id);
                            }}
                          >
                            <Plus aria-hidden />
                          </Button>
                        </span>
                      </div>
                    ))}
                  </div>
                </section>
              </>
            )}
            <p aria-live="polite" className="sr-only">{announcementText}</p>
            </>}
            {readState === 'ready' && <p className="model-hub-default-counts">{t('settings.models.routing.counts', { inherited: inheritedCount, manual: manualCount })}</p>}
          </div>

          <footer className="model-hub-order-foot flex shrink-0 items-center justify-end border-t border-border">
            {saveFailed && <span className="mr-auto text-[11px] text-destructive-ink">{t('settings.models.order.fail.save')}</span>}
            <Button type="button" variant="outline" className="model-hub-order-action" disabled={saving} onClick={() => guard ? setGuard(null) : onClose()}>
              {t('settings.models.order.cancel')}
            </Button>
            {readState === 'error'
              ? <Button type="button" variant="brand" className="model-hub-order-action" onClick={() => void read()}>{t('settings.models.order.retry')}</Button>
              : <Button type="button" variant="brand" className="model-hub-order-action" disabled={!saveEnabled || saving} onClick={() => save(guard ?? undefined)}>
                {saving && <LoaderCircle className="size-3 animate-spin" />}
                {guard ? t('settings.models.routing.confirmDefaults') : saveFailed ? t('settings.models.order.retry') : t('settings.models.order.save')}
              </Button>}
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};

export default SourceOrderDrawer;
