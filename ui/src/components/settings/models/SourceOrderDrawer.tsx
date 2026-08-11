import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { Reorder, useDragControls } from 'framer-motion';
import { GripVertical, LoaderCircle, Minus, Plus, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import type { PendingWrite } from './asyncLifetime';
import { eligibleSources } from './eligibility';
import { modelsApi } from './modelsApi';
import { movedOrder, sameIds } from './reorder';
import { sourceDetail } from './sourcePresentation';
import type { AgentSupply, Source } from './types';
import { ACCENT_PILL, sourceAccent } from './vendorMeta';

type OrderAnnouncement =
  | { key: 'grabbed' | 'moved' | 'dropped'; source: string; position: number; count: number }
  | { key: 'grabCancelled'; source: string }
  | null;

type ReadState = 'loading' | 'ready' | 'error';

const SourceIdentity: React.FC<{ source: Source }> = ({ source }) => {
  const { t } = useTranslation();
  const detail = sourceDetail(source);
  return (
    <>
      <span className="min-w-0 flex-1">
        <span className="model-hub-order-name block truncate" title={source.display_name}>{source.display_name}</span>
        {detail && <span className="model-hub-order-meta block truncate" title={detail}>{detail}</span>}
      </span>
      <span className={cn('model-hub-order-tag', ACCENT_PILL[sourceAccent(source)])}>
        {t(`settings.models.sourceKind.${source.kind}`)}
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
  onKeyDown: (event: React.KeyboardEvent<HTMLButtonElement>) => void;
}> = ({ source, index, count, busy, grabbed, handleRef, onExclude, onKeyDown }) => {
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
        className={cn('model-hub-order-grip', grabbed && 'is-grabbed')}
      >
        <GripVertical />
      </button>
      <span className={cn('model-hub-order-ordinal', index === 0 && 'is-first')}>{index + 1}</span>
      <SourceIdentity source={source} />
      <Button type="button" variant="outline" className="model-hub-order-row-action" disabled={busy} onClick={onExclude}>
        {t('settings.models.order.action.exclude')}
      </Button>
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
}> = ({ open, agent, sources, onClose, onSaved, orderWrite }) => {
  const { t } = useTranslation();
  const [viewAgent, setViewAgent] = React.useState(agent);
  const [readState, setReadState] = React.useState<ReadState>('loading');
  const [order, setOrder] = React.useState<string[]>([]);
  const [dirty, setDirty] = React.useState(false);
  const [saveFailed, setSaveFailed] = React.useState(false);
  const [grabbedId, setGrabbedId] = React.useState<string | null>(null);
  const [announcement, setAnnouncement] = React.useState<OrderAnnouncement>(null);
  const saved = React.useRef<string[]>([]);
  const grabbedFrom = React.useRef<string[]>([]);
  const handles = React.useRef(new Map<string, HTMLButtonElement>());
  const heldOutActions = React.useRef(new Map<string, HTMLButtonElement>());
  const readAttempt = React.useRef(0);
  const saving = orderWrite.pending;

  const applyRead = React.useCallback((next: AgentSupply) => {
    const nextOrder = next.sources?.order ?? [];
    setViewAgent(next);
    saved.current = nextOrder;
    setOrder(nextOrder);
    setDirty(false);
    setSaveFailed(false);
    setGrabbedId(null);
    setAnnouncement(null);
    setReadState('ready');
  }, []);

  const read = React.useCallback(async () => {
    const seq = ++readAttempt.current;
    setReadState('loading');
    try {
      const next = await modelsApi.getAgentSources(agent.backend);
      if (readAttempt.current === seq) applyRead(next);
    } catch {
      if (readAttempt.current === seq) setReadState('error');
    }
  }, [agent.backend, applyRead]);

  React.useEffect(() => {
    if (open) void read();
    else readAttempt.current += 1;
  }, [open, read]);

  const available = eligibleSources(sources, viewAgent);
  const byId = React.useMemo(() => new Map(available.map((source) => [source.id, source])), [available]);
  const ordered = order.map((id) => byId.get(id)).filter((source): source is Source => Boolean(source));
  const heldOut = available.filter((source) => !order.includes(source.id));
  const persist = (next: string[]) => {
    if (sameIds(next, order)) return;
    setOrder(next);
    setDirty(!sameIds(next, saved.current));
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
    setDirty(!sameIds(restored, saved.current));
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

  const save = () => {
    if (saving || readState !== 'ready' || available.length === 0) return;
    void orderWrite.track(async () => {
      try {
        const echoed = await modelsApi.putAgentSources(agent.backend, { order });
        saved.current = echoed.sources?.order ?? order;
        setOrder(saved.current);
        setDirty(false);
        setSaveFailed(false);
        await Promise.resolve(onSaved(echoed)).catch(() => {});
        onClose();
      } catch {
        // F1: the request failed, not the user's draft. Keep every move and let
        // the same primary retry the exact order.
        setSaveFailed(true);
      }
    });
  };

  const backend = t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend });
  const title = t('settings.models.order.title', { backend });
  const announcementText = announcement ? t(`settings.models.order.${announcement.key}`, announcement) : '';
  const saveEnabled = readState === 'ready' && available.length > 0 && (dirty || order.length === 0);

  return (
    <DialogPrimitive.Root open={open} onOpenChange={(next) => !next && !saving && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="model-hub-order-overlay fixed inset-0 z-50" />
        <DialogPrimitive.Content
          className="model-hub-order-drawer fixed inset-y-0 right-0 z-50 flex flex-col overflow-hidden bg-surface outline-none"
          onEscapeKeyDown={(event) => {
            if (grabbedId) {
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
                  label={t('settings.models.order.subtitle')}
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
            {readState === 'loading' && (
              <div className="model-hub-order-state"><LoaderCircle className="model-hub-ink-mint size-4 animate-spin" />{t('common.loading')}</div>
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
                    <span>{t('settings.models.order.section.ordered.note')}</span>
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
                  <div className="model-hub-order-section-head"><h3>{t('settings.models.order.section.heldOut')}</h3></div>
                  <div className="flex flex-col gap-2">
                    {heldOut.map((source) => (
                      <div key={source.id} className="model-hub-order-row model-hub-order-row--held">
                        <Minus className="model-hub-order-held-icon" />
                        <SourceIdentity source={source} />
                        <Button
                          ref={(node) => {
                            if (node) heldOutActions.current.set(source.id, node);
                            else heldOutActions.current.delete(source.id);
                          }}
                          type="button"
                          variant="outline"
                          className="model-hub-order-row-action"
                          disabled={saving}
                          onClick={() => {
                            persist([...order, source.id]);
                            focusHandleAfterRender(source.id);
                          }}
                        >
                          <Plus className="size-3.5" />
                          {t('settings.models.order.action.include')}
                        </Button>
                      </div>
                    ))}
                  </div>
                </section>
              </>
            )}
            <p aria-live="polite" className="sr-only">{announcementText}</p>
          </div>

          <footer className="model-hub-order-foot flex shrink-0 items-center justify-end border-t border-border">
            {saveFailed && <span className="mr-auto text-[11px] text-destructive">{t('settings.models.order.fail.save')}</span>}
            <Button type="button" variant="outline" className="model-hub-order-action" disabled={saving} onClick={onClose}>
              {t('settings.models.order.cancel')}
            </Button>
            {readState === 'error'
              ? <Button type="button" variant="brand" className="model-hub-order-action" onClick={() => void read()}>{t('settings.models.order.retry')}</Button>
              : <Button type="button" variant="brand" className="model-hub-order-action" disabled={!saveEnabled || saving} onClick={save}>
                {saving && <LoaderCircle className="size-3 animate-spin" />}
                {saveFailed ? t('settings.models.order.retry') : t('settings.models.order.save')}
              </Button>}
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};

export default SourceOrderDrawer;
