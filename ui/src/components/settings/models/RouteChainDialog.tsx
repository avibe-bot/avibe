import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import { GripVertical, Info, ListRestart, Plus, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { ModelChainRead } from './modelRows';
import { freshRegionData } from './regionRead';
import type { AgentSupply, Source } from './types';

const sourceName = (sources: Source[], sourceId: string): string =>
  sources.find((source) => source.id === sourceId)?.display_name ?? sourceId;

export type RouteChainSelection = {
  agent: AgentSupply;
  modelId: string;
  read: ModelChainRead | undefined;
};

export const RouteChainDialog: React.FC<{
  selection: RouteChainSelection | null;
  sources: Source[];
  onClose: () => void;
}> = ({ selection, sources, onClose }) => {
  const { t } = useTranslation();
  if (!selection) return null;
  const { agent, modelId, read } = selection;
  const chain = read ? freshRegionData(read) ?? null : null;
  const backend = t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend }) as string;
  return (
    <DialogPrimitive.Root open onOpenChange={(open) => !open && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="model-hub-route-overlay fixed inset-0 z-50" />
        <DialogPrimitive.Content className="model-hub-route-dialog fixed left-1/2 top-1/2 z-50 flex -translate-x-1/2 -translate-y-1/2 flex-col gap-0 overflow-hidden border border-border-strong bg-surface p-0 shadow-xl">
          <header className="model-hub-route-head flex flex-col border-b border-border">
            <span className="flex items-center justify-between gap-3">
              <DialogPrimitive.Title className="model-hub-route-title font-bold text-foreground">{t('settings.models.routeDialog.title', { model: modelId })}</DialogPrimitive.Title>
              <DialogPrimitive.Close aria-label={t('common.cancel') as string} title={t('common.cancel') as string} className="model-hub-route-close grid shrink-0 place-items-center text-muted"><X aria-hidden="true" /></DialogPrimitive.Close>
            </span>
            <DialogPrimitive.Description className="model-hub-route-subtitle font-mono text-muted">{backend}</DialogPrimitive.Description>
          </header>

          <div className="model-hub-route-body flex flex-col">
            <h3 className="model-hub-route-label font-bold text-muted">{t('settings.models.routeDialog.label')}</h3>
            <div className="model-hub-route-list flex flex-col border border-border bg-background">
              {chain ? chain.chain.map((hop, index) => {
                const current = chain.current?.source_id === hop.source_id && chain.current.model_id === hop.model_id;
                return (
                  <div key={`${hop.source_id}:${hop.model_id}:${index}`} data-current={current || undefined} className="model-hub-route-hop model-hub-fill-white-08 flex items-center border border-border">
                    <GripVertical className="model-hub-ink-white-59 size-3.5 shrink-0" aria-hidden />
                    <span className={cn('model-hub-route-ordinal grid shrink-0 place-items-center font-mono font-medium', current ? 'model-hub-accent-pill--mint' : 'model-hub-fill-white-0a text-muted')}>{index + 1}</span>
                    <span className="model-hub-route-hop-copy flex min-w-0 flex-1 flex-col"><span className="model-hub-route-hop-name truncate font-semibold text-foreground">{sourceName(sources, hop.source_id)}</span><span className="model-hub-route-hop-model model-hub-ink-muted-b3 truncate font-mono">{hop.model_id}</span></span>
                    <button type="button" disabled aria-label={t('settings.models.routeDialog.removeHop') as string} className="model-hub-route-remove model-hub-fill-white-0a grid shrink-0 place-items-center border border-border text-muted disabled:opacity-60"><X className="size-3.5" /></button>
                  </div>
                );
              }) : <div className="model-hub-route-hop model-hub-fill-white-08 grid place-items-center border border-border font-mono text-xs text-muted">—</div>}
              <button type="button" disabled className="model-hub-route-add model-hub-fill-white-05 flex w-full items-center justify-center gap-1.5 border border-border font-semibold text-muted disabled:opacity-60"><Plus className="size-3.5" />{t('settings.models.routeDialog.addHop')}</button>
            </div>
            <button type="button" disabled className="model-hub-route-reseed flex items-center gap-1.5 self-start font-semibold text-cyan disabled:opacity-60"><ListRestart className="size-3" />{t('settings.models.routeDialog.reseed')}</button>
            <p className="model-hub-route-hint flex items-start gap-2 text-muted"><Info className="mt-0.5 size-3.5 shrink-0" />{t('settings.models.routeDialog.hint')}</p>
          </div>

          <footer className="model-hub-route-foot model-hub-fill-white-05 flex items-center justify-end gap-2 border-t border-border">
            <Button variant="outline" className="model-hub-dialog-action" onClick={onClose}>{t('common.cancel')}</Button>
            <Button className="model-hub-dialog-action" disabled>{t('common.save')}</Button>
          </footer>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
};

export default RouteChainDialog;
