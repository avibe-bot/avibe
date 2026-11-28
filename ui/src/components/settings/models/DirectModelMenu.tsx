import * as React from 'react';
import { ChevronDown, ChevronUp, LoaderCircle, RotateCcw, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { COLLAPSED_MODEL_LIMIT } from './modelRows';
import type { DirectMenuRow } from './modelsApi';
import type { DirectModelMenuState } from './useDirectModelMenu';

const ModelName: React.FC<{ row: DirectMenuRow; muted?: boolean }> = ({ row, muted = false }) => (
  <span className="flex min-w-0 flex-1 flex-col justify-center gap-0.5">
    {row.label !== row.id && <span className={cn('min-w-0 truncate text-[12.5px] font-semibold', muted ? 'text-muted' : 'text-foreground')} title={row.label}>{row.label}</span>}
    <span className={cn('min-w-0 truncate font-mono', row.label !== row.id ? 'text-[10.5px] text-muted' : cn('text-[12px] font-medium', muted ? 'text-muted' : 'text-foreground'))} title={row.id}>{row.id}</span>
  </span>
);

const ACTION = 'rounded-md px-2.5 text-[11px] font-semibold';

export const DirectModelList: React.FC<{ menu: DirectModelMenuState }> = ({ menu }) => {
  const { t } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);
  const [removedOpen, setRemovedOpen] = React.useState(false);
  const { models, unread, failed, removed, canEdit, pending, writeFailure, setRemoved, read } = menu;
  if (models === null) {
    if (failed) {
      return (
        <div className="flex flex-col items-center gap-3 px-4 py-8 text-center">
          <p className="text-[12.5px] text-muted">{t('settings.models.gateway.direct.readFailed')}</p>
          <Button variant="outline" size="xs" className={ACTION} onClick={() => read(unread)}>{t('settings.models.gateway.retry')}</Button>
        </div>
      );
    }
    if (unread) {
      return (
        <div className="flex justify-center px-4 py-6">
          <Button variant="outline" size="xs" className={ACTION} onClick={() => read(true)}>{t('settings.models.gateway.direct.readLive')}</Button>
        </div>
      );
    }
    return <div className="flex justify-center py-6" data-direct-models-pending><LoaderCircle className="size-4 animate-spin text-muted" aria-hidden="true" /></div>;
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
      {models.length > COLLAPSED_MODEL_LIMIT && (
        <button type="button" onClick={() => setExpanded((value) => !value)} className="model-hub-model-collapse flex h-6 w-full items-center gap-1.5 hover:text-foreground">
          {expanded ? <ChevronUp /> : <ChevronDown />}
          {expanded ? t('settings.models.gateway.collapse') : t('settings.models.gateway.moreModels', { count: hidden })}
        </button>
      )}
      {writeFailure && (
        <p role="status" className="px-3 text-[11px] font-semibold text-destructive-ink">
          {t(writeFailure.removed ? 'settings.models.gateway.direct.failRemove' : 'settings.models.gateway.direct.failRestore', { model: writeFailure.row.label })}
        </p>
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
                    className={cn('shrink-0', ACTION)}
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
