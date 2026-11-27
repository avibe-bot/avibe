import * as React from 'react';
import { ChevronDown, ChevronUp, RotateCcw, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { useApi } from '@/context/ApiContext';
import { useInstanceAuthorization } from '@/context/InstanceAuthorizationContext';
import { useToast } from '@/context/ToastContext';
import { loadBackendModelsWithRefresh, modelOptionLabel } from '@/lib/backendModels';
import { cn } from '@/lib/utils';
import { COLLAPSED_MODEL_LIMIT } from './modelRows';

type Row = { id: string; label: string };

export type DirectModelMenuState = {
  /** The menu the Session model picker shows for this backend; null until the first answer. */
  models: Row[] | null;
  failed: boolean;
  /** Rows the backend offers now that the menu hides: the restore list. */
  removed: Row[];
  canEdit: boolean;
  pending: ReadonlySet<string>;
  setRemoved: (row: Row, removed: boolean) => void;
  retry: () => void;
};

const NO_ROWS: Row[] = [];

/**
 * A Direct backend's model menu as Settings · Models shows it: the same read as
 * the Session model picker (`loadBackendModelsWithRefresh`), so both list the
 * same models. Whoever may add models (`can_manage_instance`) removes a model from
 * the menu here, or brings back one it removed; removing hides the model from
 * every menu, and a Session or Agent already on it keeps it.
 * ui/design: models/direct-menu/list
 */
export function useDirectModelMenu(backend: string, enabled: boolean): DirectModelMenuState {
  const api = useApi();
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { capabilities } = useInstanceAuthorization();
  const canEdit = capabilities.can_manage_instance;
  const [models, setModels] = React.useState<Row[] | null>(null);
  const [failed, setFailed] = React.useState(false);
  const [removed, setRemovedRows] = React.useState<Row[]>(NO_ROWS);
  const [pending, setPending] = React.useState<ReadonlySet<string>>(new Set());
  const [generation, setGeneration] = React.useState(0);

  React.useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    setFailed(false);
    const cancel = loadBackendModelsWithRefresh(
      api,
      backend,
      (result) => {
        setModels(result.models.map((id) => ({ id, label: modelOptionLabel(id, result.modelLabels) })));
        setFailed(false);
      },
      () => setFailed(true),
    );
    if (canEdit) {
      void api.readDirectModelRemovals(backend).then(
        (answer) => { if (!cancelled) setRemovedRows(answer?.ok && Array.isArray(answer.models) ? answer.models : NO_ROWS); },
        () => { if (!cancelled) setRemovedRows(NO_ROWS); },
      );
    }
    return () => {
      cancelled = true;
      cancel();
    };
  }, [api, backend, canEdit, enabled, generation]);

  const setRemoved = React.useCallback((row: Row, hide: boolean) => {
    setPending((current) => new Set(current).add(row.id));
    void api.setDirectModelRemoved(backend, row.id, hide).then(
      () => setGeneration((value) => value + 1),
      () => showToast(t(hide ? 'settings.models.gateway.direct.failRemove' : 'settings.models.gateway.direct.failRestore', { model: row.label }) as string, 'error'),
    ).finally(() => setPending((current) => {
      const next = new Set(current);
      next.delete(row.id);
      return next;
    }));
  }, [api, backend, showToast, t]);

  const retry = React.useCallback(() => setGeneration((value) => value + 1), []);
  return { models, failed, removed: canEdit ? removed : NO_ROWS, canEdit, pending, setRemoved, retry };
}

const ModelName: React.FC<{ row: Row; muted?: boolean }> = ({ row, muted = false }) => (
  <span className="flex min-w-0 flex-1 flex-col justify-center gap-0.5">
    {row.label !== row.id && <span className={cn('min-w-0 truncate text-[12.5px] font-semibold', muted ? 'text-muted' : 'text-foreground')} title={row.label}>{row.label}</span>}
    <span className={cn('min-w-0 truncate font-mono', row.label !== row.id ? 'text-[10.5px] text-muted' : cn('text-[12px] font-medium', muted ? 'text-muted' : 'text-foreground'))} title={row.id}>{row.id}</span>
  </span>
);

export const DirectModelList: React.FC<{ menu: DirectModelMenuState }> = ({ menu }) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);
  const [removedOpen, setRemovedOpen] = React.useState(false);
  const { models, failed, removed, canEdit, pending, setRemoved, retry } = menu;
  if (models === null) {
    return failed ? (
      <div className="flex flex-col items-center gap-3 px-4 py-8 text-center">
        <p className="text-[12.5px] text-muted">{t('settings.models.gateway.direct.readFailed')}</p>
        <Button variant="outline" size="xs" className="rounded-md px-2.5 text-[11px] font-semibold" onClick={retry}>{t('settings.models.gateway.retry')}</Button>
      </div>
    ) : null;
  }
  const visible = expanded ? models : models.slice(0, COLLAPSED_MODEL_LIMIT);
  const hidden = models.length - visible.length;
  return (
    <div className="space-y-2 p-2" data-direct-models>
      {models.length === 0 && <p className="px-3 py-6 text-center text-[12.5px] text-muted">{t('settings.models.gateway.group.emptyModels')}</p>}
      {visible.map((row) => (
        <div key={row.id} data-direct-model={row.id} className="flex h-[52px] w-full min-w-0 items-center gap-2.5 rounded-md border border-edge px-3 shadow-elevation-0">
          <ModelName row={row} />
          {canEdit && (
            <Button
              variant="ghost"
              size="icon"
              className="size-8 shrink-0 rounded-md text-muted hover:text-foreground"
              aria-label={t('settings.models.gateway.catalog.remove', { model: row.label }) as string}
              disabled={pending.has(row.id)}
              onClick={() => setRemoved(row, true)}
            >
              <Trash2 className="size-3.5" aria-hidden="true" />
            </Button>
          )}
        </div>
      ))}
      {(hidden > 0 || expanded) && models.length > COLLAPSED_MODEL_LIMIT && (
        <button type="button" onClick={() => setExpanded((value) => !value)} className="model-hub-model-collapse flex h-6 w-full items-center gap-1.5 hover:text-foreground">
          {expanded ? <ChevronUp /> : <ChevronDown />}
          {expanded ? t('settings.models.gateway.collapse') : t('settings.models.gateway.moreModels', { count: hidden })}
        </button>
      )}
      {removed.length > 0 && (
        <div className="border-t border-border pt-2">
          <button
            type="button"
            data-direct-removed-toggle
            aria-expanded={removedOpen}
            onClick={() => setRemovedOpen((value) => !value)}
            className="model-hub-model-collapse flex h-6 w-full items-center gap-1.5 hover:text-foreground"
          >
            {removedOpen ? <ChevronUp /> : <ChevronDown />}
            {t('settings.models.gateway.direct.removed', { count: removed.length })}
          </button>
          {removedOpen && (
            <div className="mt-2 space-y-2">
              {removed.map((row) => (
                <div key={row.id} data-direct-removed-model={row.id} className="flex h-[52px] w-full min-w-0 items-center gap-2.5 rounded-md border border-dashed border-edge px-3">
                  <ModelName row={row} muted />
                  <Button
                    variant="outline"
                    size="xs"
                    className="shrink-0 rounded-md px-2.5 text-[11px] font-semibold"
                    aria-label={t('settings.models.gateway.direct.restoreModel', { model: row.label }) as string}
                    disabled={pending.has(row.id)}
                    onClick={() => setRemoved(row, false)}
                  >
                    <RotateCcw aria-hidden="true" />
                    {t('settings.models.gateway.direct.restore')}
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};
