// One model-catalog dialog for every backend.
//
// It replaces OpenCodeMenuDialog, which existed because OpenCode was the only
// backend whose menu the user could edit. That distinction was never a product
// idea — it was the shape of the old data. With one catalog per backend, one
// dialog edits it, and Claude Code's `Default` is not a special case in the UI
// but an ordinary row the server marked `locked`.
//
// It shows the catalog and nothing else: no Source, no Route, no fallback, no
// mapping. Which supplier serves a model is a Route question, and the Route rows
// on the Agent card are where it stays answered.
import * as React from 'react';
import { Reorder, useDragControls } from 'framer-motion';
import { GripVertical, Lock, LoaderCircle, Pencil, Plus, Search, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import type { PendingWrite } from './asyncLifetime';
import {
  applyBackendCatalogIntent,
  backendCatalogIntent,
  backendCatalogIntentApplied,
  catalogModelIds,
  readBackendCatalogBaseline,
  sameCatalog,
  type BackendCatalogBaseline,
} from './backendCatalog';
import { BackendModelEditorDialog } from './BackendModelEditorDialog';
import { apiFailure, modelsApi } from './modelsApi';
import { movedOrder, sameIds } from './reorder';
import { catalogSaveFailureKey, catalogSaveLeftUnloaded } from './serverCopy';
import type { AgentBackend, AgentSupply, BackendModel } from './types';

type ReadState = 'loading' | 'ready' | 'error';

/** Same shape the Source order drawer announces with: the key alone would tell a
 *  screen-reader user something moved without telling them where to. */
type AnnouncementKey = 'grabbed' | 'moved' | 'dropped' | 'grabCancelled';

type Announcement = {
  key: AnnouncementKey;
  model: string;
  position: number;
  count: number;
} | null;

const matchesQuery = (model: BackendModel, query: string, renderedLabel: string): boolean => {
  if (query === '') return true;
  const needle = query.toLowerCase();
  return model.id.toLowerCase().includes(needle)
    || renderedLabel.toLowerCase().includes(needle);
};

export const BackendModelCatalogDialog: React.FC<{
  open: boolean;
  backend: AgentBackend;
  onClose: () => void;
  onSaved: (echoed: AgentSupply) => void | Promise<void>;
  onObserved: (observed: AgentSupply) => void | Promise<void>;
  catalogWrite: PendingWrite;
}> = ({ open, backend, onClose, onSaved, onObserved, catalogWrite }) => {
  const { t } = useTranslation();
  const [baseline, setBaseline] = React.useState<BackendCatalogBaseline | null>(null);
  const baselineRef = React.useRef<BackendCatalogBaseline | null>(null);
  const [readState, setReadState] = React.useState<ReadState>('loading');
  const [draft, setDraft] = React.useState<BackendModel[]>([]);
  const draftRef = React.useRef<BackendModel[]>([]);
  const [query, setQuery] = React.useState('');
  const [saveFailedKey, setSaveFailedKey] = React.useState<string | null>(null);
  const [editing, setEditing] = React.useState<{ model: BackendModel | null } | null>(null);
  const [removeBlocked, setRemoveBlocked] = React.useState<string | null>(null);
  const [grabbedId, setGrabbedId] = React.useState<string | null>(null);
  const [announcement, setAnnouncement] = React.useState<Announcement>(null);
  const readAttempt = React.useRef(0);
  const grips = React.useRef(new Map<string, HTMLButtonElement>());
  const grabbedFrom = React.useRef<string[] | null>(null);

  const applyBaseline = React.useCallback((observed: BackendCatalogBaseline, models: BackendModel[]) => {
    baselineRef.current = observed;
    setBaseline(observed);
    draftRef.current = models;
    setDraft(models);
    setReadState('ready');
  }, []);

  const loadBaseline = React.useCallback((preserveDraft: boolean) => {
    const attempt = ++readAttempt.current;
    setReadState('loading');
    void (async () => {
      try {
        const observed = await readBackendCatalogBaseline(modelsApi, backend);
        if (attempt !== readAttempt.current) return;
        const server = observed.models ?? [];
        const previous = baselineRef.current?.models;
        const next = preserveDraft && previous
          ? applyBackendCatalogIntent(server, backendCatalogIntent(previous, draftRef.current))
          : server;
        applyBaseline(observed, next);
      } catch {
        if (attempt === readAttempt.current) setReadState('error');
      }
    })();
  }, [applyBaseline, backend]);

  React.useEffect(() => {
    if (open) loadBaseline(false);
    return () => { readAttempt.current += 1; };
  }, [loadBaseline, open]);

  const mutate = (next: BackendModel[]) => {
    draftRef.current = next;
    setDraft(next);
    setRemoveBlocked(null);
    setSaveFailedKey(null);
  };

  // The server sent no `catalog_models`, so this build is talking to a release
  // that predates the catalog. The legacy list is still worth showing — it is
  // what the backend really exposes — but the dialog will not offer to write a
  // baseline it assembled itself.
  const legacy = baseline !== null && baseline.models === null;
  const legacyIds = legacy && baseline ? catalogModelIds(baseline.agent) : [];
  const ready = readState === 'ready' && baseline !== null;
  const editable = ready && !legacy;
  const busy = catalogWrite.pending;
  const dirty = editable && !sameCatalog(baseline?.models ?? [], draft);
  /** A save that failed after the re-read rebased the draft leaves nothing to
   *  send and everything still to do — a catalog the backend never loaded reads
   *  as settled, so pressing Save again has to stay possible. */
  const retryable = editable && saveFailedKey !== null;
  const filtering = query.trim() !== '';
  const displayLabel = (model: BackendModel): string => (
    backend === 'claude' && model.id === 'default' && model.locked && !model.routeable
      ? t('settings.models.gateway.catalog.systemDefault') as string
      : model.display_name ?? model.id
  );
  const visible = draft.filter((model) => matchesQuery(model, query.trim(), displayLabel(model)));
  const movableIds = draft.filter((model) => !model.locked).map((model) => model.id);
  const takenIds = new Set(draft.map((model) => model.id));
  const effortSuggestions = [...new Set(draft.flatMap((model) => model.reasoning_efforts))];
  const routeLength = (modelId: string): number => baseline?.agent.routes?.[modelId]?.hops.length ?? 0;

  /** Reordering permutes which movable row sits in which movable slot; a locked
   *  row keeps its absolute index, so the order sent never moves one. */
  const reorderMovable = (nextIds: string[]) => {
    const byId = new Map(draft.map((model) => [model.id, model]));
    let cursor = 0;
    mutate(draft.map((model) => (model.locked ? model : byId.get(nextIds[cursor++]) as BackendModel)));
  };

  const label = (modelId: string): string => {
    const model = draft.find((entry) => entry.id === modelId);
    return model ? displayLabel(model) : modelId;
  };

  const announce = (key: AnnouncementKey, modelId: string, order: string[] = movableIds) => {
    setAnnouncement({
      key,
      model: label(modelId),
      position: order.indexOf(modelId) + 1,
      count: order.length,
    });
  };

  const focusGrip = (modelId: string) => {
    requestAnimationFrame(() => grips.current.get(modelId)?.focus());
  };

  const cancelGrab = () => {
    const restored = grabbedFrom.current;
    grabbedFrom.current = null;
    const focused = grabbedId;
    setGrabbedId(null);
    if (restored && !sameIds(restored, movableIds)) reorderMovable(restored);
    if (focused) {
      announce('grabCancelled', focused, restored ?? movableIds);
      focusGrip(focused);
    }
  };

  const handleGripKey = (modelId: string, event: React.KeyboardEvent<HTMLButtonElement>) => {
    const index = movableIds.indexOf(modelId);
    if (index < 0) return;
    if (event.key === ' ') {
      event.preventDefault();
      if (grabbedId === modelId) {
        grabbedFrom.current = null;
        setGrabbedId(null);
        announce('dropped', modelId);
      } else {
        grabbedFrom.current = movableIds;
        setGrabbedId(modelId);
        announce('grabbed', modelId);
      }
      return;
    }
    if (event.key === 'Escape' && grabbedId === modelId) {
      event.preventDefault();
      event.stopPropagation();
      cancelGrab();
      return;
    }
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    event.preventDefault();
    const delta = event.key === 'ArrowUp' ? -1 : 1;
    if (grabbedId === modelId) {
      const next = movedOrder(movableIds, index, delta);
      if (sameIds(next, movableIds)) return;
      reorderMovable(next);
      announce('moved', modelId, next);
      focusGrip(modelId);
      return;
    }
    const neighbour = movableIds[index + delta];
    if (neighbour) focusGrip(neighbour);
  };

  const removeModel = (model: BackendModel) => {
    if (routeLength(model.id) > 0) {
      setRemoveBlocked(model.id);
      return;
    }
    mutate(draft.filter((entry) => entry.id !== model.id));
  };

  const commitEdit = (model: BackendModel) => {
    const existing = draft.findIndex((entry) => entry.id === model.id);
    mutate(existing >= 0
      ? draft.map((entry, index) => (index === existing ? { ...model, locked: entry.locked, routeable: entry.routeable } : entry))
      : [...draft, model]);
    setEditing(null);
  };

  const save = () => {
    const base = baselineRef.current;
    // A pre-catalog server has no baseline to send, so there is nothing this
    // branch could honestly PUT.
    if (!base || base.models === null) return;
    const baselineModels = base.models;
    const requested = draftRef.current;
    const intent = backendCatalogIntent(baselineModels, requested);
    setSaveFailedKey(null);
    void catalogWrite.track(async () => {
      let echoed: AgentSupply;
      try {
        echoed = await modelsApi.putAgentModels(backend, { baseline: baselineModels, models: requested });
      } catch (error) {
        const failure = apiFailure(error);
        // A route that named its failure has decided what it did, and for this
        // endpoint 「decided」 can still mean 「wrote」: the server commits the
        // catalog and only then asks the backend to load it, so `engine_down`
        // names rows that reached the disk and never reached the runtime. That
        // is a failed save with a persisted list behind it, and no re-read can
        // turn it into a success. Only an answer this client never got to read
        // leaves the outcome genuinely unknown — and only that may be settled by
        // reading the server's own catalog back.
        const decided = failure?.serverNamed === true;
        const unloaded = decided && catalogSaveLeftUnloaded(failure?.code, failure?.detail);
        const reason = decided
          ? catalogSaveFailureKey(failure?.detail)
          : 'settings.models.gateway.catalog.saveFailed';
        try {
          const observed = await readBackendCatalogBaseline(modelsApi, backend);
          const current = observed.models;
          const landed = current !== null && backendCatalogIntentApplied(current, intent);
          if (current && landed && !decided) {
            applyBaseline(observed, current);
            await Promise.resolve(onSaved(observed.agent)).catch(() => {});
            onClose();
            return;
          }
          applyBaseline(observed, current ? applyBackendCatalogIntent(current, intent) : []);
          await Promise.resolve(onObserved(observed.agent)).catch(() => {});
          // Stored and out of use at the same time, and only for the code that
          // can leave a list that way: a validation or conflict refusal keeps
          // its own sentence even when the re-read happens to agree with the
          // draft, because that write never landed at all.
          setSaveFailedKey(landed && unloaded ? 'settings.models.gateway.catalog.saveNotApplied' : reason);
          return;
        } catch {
          setReadState('error');
        }
        setSaveFailedKey(reason);
        return;
      }
      await Promise.resolve(onSaved(echoed)).catch(() => {});
      onClose();
    });
  };

  const rowActions = (model: BackendModel) => (
    <div className="flex shrink-0 items-center gap-1.5">
      <button
        type="button"
        className="model-hub-catalog-action"
        aria-label={t('settings.models.gateway.catalog.edit', { model: model.display_name ?? model.id }) as string}
        disabled={busy}
        onClick={() => setEditing({ model })}
      >
        <Pencil className="size-[15px]" aria-hidden="true" />
      </button>
      <button
        type="button"
        className="model-hub-catalog-action model-hub-catalog-action--danger"
        aria-label={t('settings.models.gateway.catalog.remove', { model: model.display_name ?? model.id }) as string}
        disabled={busy}
        onClick={() => removeModel(model)}
      >
        <Trash2 className="size-[15px]" aria-hidden="true" />
      </button>
    </div>
  );

  const rowBody = (model: BackendModel) => {
    const renderedLabel = displayLabel(model);
    return (
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="model-hub-catalog-name truncate">{renderedLabel}</span>
        {renderedLabel !== model.id && <span className="model-hub-catalog-id truncate font-mono">{model.id}</span>}
      </span>
    );
  };

  const blockedNote = (model: BackendModel) => (
    removeBlocked === model.id
      ? <p className="model-hub-catalog-blocked" role="alert">{t('settings.models.gateway.catalog.removeBlocked')}</p>
      : null
  );

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(next) => { if (!next && !busy) onClose(); }}
      >
        <DialogContent
          mobileSheetHeight="tall"
          closeLabel={t('settings.models.gateway.catalog.cancel') as string}
          className="model-hub-catalog-dialog flex h-[min(642px,calc(100dvh-32px))] w-[min(680px,calc(100vw-32px))] max-w-[680px] flex-col gap-0 overflow-hidden rounded-[14px] border-border-strong bg-surface p-0 shadow-[var(--model-hub-dialog-shadow)] max-md:w-full max-md:max-w-none max-md:rounded-t-2xl max-md:p-0 max-md:pt-2"
          onEscapeKeyDown={(event) => { if (busy || grabbedId) event.preventDefault(); }}
          onPointerDownOutside={(event) => { if (busy) event.preventDefault(); }}
        >
          <DialogHeader className="model-hub-catalog-head shrink-0 justify-center border-b border-border">
            <DialogTitle className="model-hub-catalog-title">
              {t('settings.models.gateway.catalog.title', { backend: t(`settings.models.backends.${backend}`) })}
            </DialogTitle>
            <DialogDescription className="sr-only">{t('settings.models.gateway.catalog.description')}</DialogDescription>
          </DialogHeader>

          <div className="model-hub-catalog-body flex min-h-0 flex-1 flex-col">
            <div className="flex min-w-0 shrink-0 flex-col gap-2 sm:flex-row sm:items-center">
              <div className="model-hub-catalog-control model-hub-catalog-search flex min-w-0 flex-1 items-center gap-2">
                <Search className="size-4 shrink-0 text-muted" aria-hidden="true" />
                <Input
                  variant="bare"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder={t('settings.models.gateway.catalog.search') as string}
                  aria-label={t('settings.models.gateway.catalog.search') as string}
                  className="min-w-0 flex-1 text-[12.5px]"
                  disabled={!editable || busy}
                />
              </div>
              <Button
                type="button"
                variant="brand"
                className="model-hub-catalog-control shrink-0 rounded-md px-4 text-[12.5px] font-semibold"
                disabled={!editable || busy}
                onClick={() => setEditing({ model: null })}
              >
                <Plus aria-hidden="true" />
                {t('settings.models.gateway.catalog.add')}
              </Button>
            </div>

            {ready && (draft.length > 0 || legacyIds.length > 0) && (
              <div className="model-hub-catalog-columns flex shrink-0 items-center justify-between">
                <span>{t('settings.models.gateway.catalog.columnModel')}</span>
                <span>{legacy ? t('settings.models.gateway.catalog.columnState') : t('settings.models.gateway.catalog.columnActions')}</span>
              </div>
            )}

            <div className="min-h-0 flex-1 overflow-y-auto">
              {!ready ? (
                <div className="flex flex-col items-center gap-3 px-4 py-12 text-center text-[12.5px] text-muted">
                  {readState === 'loading' ? (
                    <LoaderCircle className="size-5 animate-spin" aria-hidden="true" />
                  ) : (
                    <>
                      <p>{t('settings.models.gateway.catalog.baselineUnavailable')}</p>
                      <Button type="button" variant="outline" size="sm" onClick={() => loadBaseline(true)}>
                        {t('settings.models.gateway.retry')}
                      </Button>
                    </>
                  )}
                </div>
              ) : legacy ? (
                <div className="flex flex-col gap-2">
                  <p className="model-hub-catalog-legacy">{t('settings.models.gateway.catalog.legacy')}</p>
                  {legacyIds.map((modelId) => (
                    <div key={modelId} className="model-hub-catalog-row model-hub-catalog-row--static flex min-w-0 items-center gap-3">
                      <span className="model-hub-catalog-name min-w-0 flex-1 truncate font-mono">{modelId}</span>
                    </div>
                  ))}
                </div>
              ) : visible.length === 0 ? (
                <p className="px-1 py-10 text-center text-[12.5px] text-muted">
                  {t(filtering ? 'settings.models.gateway.catalog.noMatch' : 'settings.models.gateway.catalog.empty')}
                </p>
              ) : (
                <Reorder.Group
                  axis="y"
                  as="ul"
                  values={movableIds}
                  onReorder={reorderMovable}
                  className="flex list-none flex-col gap-[var(--model-hub-catalog-row-gap)] p-0"
                >
                  {visible.map((model) => (
                    model.locked
                      ? (
                        <li key={model.id} className="model-hub-catalog-row model-hub-catalog-row--locked flex min-w-0 flex-col justify-center">
                          <div className="flex min-w-0 items-center gap-3">
                            <Lock className="model-hub-catalog-grip-icon shrink-0" aria-hidden="true" />
                            {rowBody(model)}
                            <span className="model-hub-pill model-hub-fill-0a shrink-0 border border-border text-muted">
                              {t('settings.models.gateway.catalog.lockedBadge')}
                            </span>
                          </div>
                        </li>
                      )
                      : (
                        <CatalogRow
                          key={model.id}
                          model={model}
                          grabbed={grabbedId === model.id}
                          draggable={!filtering && !busy}
                          registerGrip={(node) => {
                            if (node) grips.current.set(model.id, node);
                            else grips.current.delete(model.id);
                          }}
                          onGripKeyDown={(event) => handleGripKey(model.id, event)}
                          body={rowBody(model)}
                          actions={rowActions(model)}
                          note={blockedNote(model)}
                        />
                      )
                  ))}
                </Reorder.Group>
              )}
            </div>
            <p aria-live="polite" className="sr-only">
              {announcement ? t(`settings.models.gateway.catalog.${announcement.key}`, announcement) : ''}
            </p>
          </div>

          <DialogFooter className="model-hub-catalog-foot shrink-0 items-center border-t border-border sm:justify-between">
            <div className="flex min-w-0 flex-col gap-0.5">
              <span className="model-hub-catalog-count">
                {t('settings.models.gateway.modelCount', { count: legacy ? legacyIds.length : draft.length })}
              </span>
              {saveFailedKey && (
                <span className="model-hub-catalog-blocked" role="status">{t(saveFailedKey)}</span>
              )}
            </div>
            <div className="flex w-full flex-col-reverse gap-2 sm:w-auto sm:flex-row">
              <Button
                type="button"
                variant="outline"
                className="model-hub-catalog-control rounded-md px-5 text-[12.5px] font-semibold"
                onClick={onClose}
                disabled={busy}
              >
                {t('settings.models.gateway.catalog.cancel')}
              </Button>
              <Button
                type="button"
                variant="brand"
                className="model-hub-catalog-control rounded-md px-5 text-[12.5px] font-semibold"
                onClick={save}
                disabled={!editable || (!dirty && !retryable) || busy}
              >
                {busy && <LoaderCircle className="animate-spin" aria-hidden="true" />}
                {t(busy ? 'settings.models.gateway.catalog.saving' : 'settings.models.gateway.catalog.save')}
              </Button>
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {editing && (
        <BackendModelEditorDialog
          open
          backend={backend}
          model={editing.model}
          takenIds={takenIds}
          effortSuggestions={effortSuggestions}
          onCancel={() => setEditing(null)}
          onCommit={commitEdit}
        />
      )}
    </>
  );
};

const CatalogRow: React.FC<{
  model: BackendModel;
  grabbed: boolean;
  draggable: boolean;
  registerGrip: (node: HTMLButtonElement | null) => void;
  onGripKeyDown: (event: React.KeyboardEvent<HTMLButtonElement>) => void;
  body: React.ReactNode;
  actions: React.ReactNode;
  note: React.ReactNode;
}> = ({ model, grabbed, draggable, registerGrip, onGripKeyDown, body, actions, note }) => {
  const { t } = useTranslation();
  const controls = useDragControls();
  return (
    <Reorder.Item
      value={model.id}
      dragListener={false}
      dragControls={controls}
      className="model-hub-catalog-row flex min-w-0 list-none flex-col justify-center"
    >
      <div className="flex min-w-0 items-center gap-3">
        <button
          ref={registerGrip}
          type="button"
          aria-label={t('settings.models.gateway.catalog.reorder', { model: model.display_name ?? model.id }) as string}
          aria-grabbed={grabbed}
          disabled={!draggable}
          onPointerDown={(event) => { if (draggable) controls.start(event); }}
          onKeyDown={onGripKeyDown}
          className={cn(
            'model-hub-catalog-grip cursor-grab active:cursor-grabbing disabled:cursor-not-allowed',
            grabbed && 'is-grabbed',
          )}
        >
          <GripVertical className="model-hub-catalog-grip-icon" aria-hidden="true" />
        </button>
        {body}
        {actions}
      </div>
      {note}
    </Reorder.Item>
  );
};

export default BackendModelCatalogDialog;
