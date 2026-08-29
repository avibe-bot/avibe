import * as React from 'react';
import { LoaderCircle, Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { initialSeedState, savedMenuKey, seedStep, type PendingWrite } from './asyncLifetime';
import { eligibleSources } from './eligibility';
import { buildIdentifier } from './menus/identifiers';
import { modelsApi } from './modelsApi';
import type { AgentSupply, Source } from './types';

type MenuModel = {
  id: string;
  sourceNames: string[];
};

const selectableModels = (agent: AgentSupply, sources: Source[]): MenuModel[] => {
  const standardVendors = new Set(agent.standard_vendors ?? []);
  const namesById = new Map<string, Set<string>>();
  for (const source of eligibleSources(sources, agent)) {
    for (const model of source.models) {
      if (model.retired) continue;
      const id = buildIdentifier(source.vendor, model.id, standardVendors);
      const names = namesById.get(id) ?? new Set<string>();
      names.add(source.display_name);
      namesById.set(id, names);
    }
  }
  return [...namesById.entries()]
    .map(([id, names]) => ({ id, sourceNames: [...names].sort((left, right) => left.localeCompare(right)) }))
    .sort((left, right) => left.id.localeCompare(right.id));
};

export const OpenCodeMenuDialog: React.FC<{
  open: boolean;
  agent: AgentSupply;
  sources: Source[];
  onClose: () => void;
  onSaved: (echoed: AgentSupply) => void | Promise<void>;
  menuWrite: PendingWrite;
}> = ({ open, agent, sources, onClose, onSaved, menuWrite }) => {
  const { t } = useTranslation();
  const models = React.useMemo(() => selectableModels(agent, sources), [agent, sources]);
  const availableIds = React.useMemo(() => new Set(models.map((model) => model.id)), [models]);
  const rawChecked = React.useMemo(() => agent.menu?.checked ?? [], [agent.menu?.checked]);
  const savedChecked = React.useMemo(
    () => (agent.menu?.checked ?? []).filter((id) => availableIds.has(id)),
    [agent.menu?.checked, availableIds],
  );
  const [selected, setSelected] = React.useState<Set<string>>(() => new Set(savedChecked));
  const [query, setQuery] = React.useState('');
  const [saveFailed, setSaveFailed] = React.useState(false);
  const seed = React.useRef(initialSeedState);

  const authoritative = savedMenuKey(agent.menu);
  React.useEffect(() => {
    if (!open) return;
    const step = seedStep(seed.current, authoritative);
    seed.current = step.state;
    if (!step.reseed) return;
    setSelected(new Set(savedChecked));
    setQuery('');
    setSaveFailed(false);
  }, [open, authoritative, savedChecked]);

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleModels = normalizedQuery
    ? models.filter((model) => `${model.id} ${model.sourceNames.join(' ')}`.toLocaleLowerCase().includes(normalizedQuery))
    : models;
  const savedSet = new Set(rawChecked);
  const dirty = selected.size !== savedSet.size || [...selected].some((id) => !savedSet.has(id));

  const toggle = (id: string) => {
    if (menuWrite.pending) return;
    setSaveFailed(false);
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const save = () => {
    if (!dirty || menuWrite.pending) return;
    setSaveFailed(false);
    void menuWrite.track(async () => {
      let echoed: AgentSupply;
      try {
        const existing = rawChecked.filter((id) => availableIds.has(id) && selected.has(id));
        const added = models.map((model) => model.id).filter((id) => selected.has(id) && !savedSet.has(id));
        echoed = await modelsApi.putMenu({
          view: agent.menu?.view ?? 'featured',
          checked: [...existing, ...added],
        });
      } catch {
        setSaveFailed(true);
        return;
      }
      await Promise.resolve(onSaved(echoed)).catch(() => {});
      onClose();
    });
  };

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { if (!nextOpen && !menuWrite.pending) onClose(); }}>
      <DialogContent
        mobileSheetHeight="tall"
        closeLabel={t('settings.models.gateway.menu.cancel') as string}
        className="flex h-[min(640px,calc(100dvh-32px))] w-[min(680px,calc(100vw-32px))] max-w-[680px] flex-col gap-4 overflow-hidden rounded-[14px] border-border-strong bg-surface p-5 shadow-[var(--model-hub-dialog-shadow)] max-md:w-full max-md:max-w-none max-md:rounded-t-2xl"
        onEscapeKeyDown={(event) => { if (menuWrite.pending) event.preventDefault(); }}
        onPointerDownOutside={(event) => { if (menuWrite.pending) event.preventDefault(); }}
      >
        <DialogHeader>
          <DialogTitle>{t('settings.models.gateway.menu.title')}</DialogTitle>
          <DialogDescription className="sr-only">{t('settings.models.gateway.menu.description')}</DialogDescription>
        </DialogHeader>

        <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center">
          <div className="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-md border border-border bg-background px-3 focus-within:ring-2 focus-within:ring-ring">
            <Search className="size-4 shrink-0 text-muted" aria-hidden="true" />
            <Input
              variant="bare"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t('settings.models.gateway.menu.search') as string}
              aria-label={t('settings.models.gateway.menu.search') as string}
              className="min-w-0 flex-1 text-[12.5px]"
              disabled={menuWrite.pending}
            />
          </div>
          <span className="shrink-0 text-[11px] font-semibold text-muted">{t('settings.models.gateway.menu.selected', { selected: selected.size, total: models.length })}</span>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto rounded-md border border-border bg-background p-1.5">
          {models.length === 0 ? (
            <p className="px-4 py-10 text-center text-[12.5px] text-muted">{t('settings.models.gateway.menu.empty')}</p>
          ) : visibleModels.length === 0 ? (
            <p className="px-4 py-10 text-center text-[12.5px] text-muted">{t('settings.models.gateway.menu.noMatch')}</p>
          ) : (
            <div className="space-y-1">
              {visibleModels.map((model) => {
                const checked = selected.has(model.id);
                return (
                  <button
                    key={model.id}
                    type="button"
                    role="checkbox"
                    aria-checked={checked}
                    disabled={menuWrite.pending}
                    onClick={() => toggle(model.id)}
                    className={cn(
                      'flex min-h-11 w-full min-w-0 items-center gap-3 rounded-md px-3 py-2 text-left transition-colors hover:bg-surface-2 disabled:opacity-50',
                      checked && 'bg-surface-2',
                    )}
                  >
                    <Checkbox checked={checked} presentational />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-[12px] font-medium text-foreground" title={model.id}>{model.id}</span>
                      <span className="mt-0.5 block truncate text-[10.5px] text-muted" title={model.sourceNames.join(', ')}>{model.sourceNames.join(' · ')}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <DialogFooter className="items-center sm:justify-between">
          <div className="min-h-5 text-[11px] font-semibold text-destructive-ink" role="status">{saveFailed ? t('settings.models.gateway.menu.saveFailed') : null}</div>
          <div className="flex w-full flex-col-reverse gap-2 sm:w-auto sm:flex-row">
            <Button type="button" variant="outline" onClick={onClose} disabled={menuWrite.pending}>{t('settings.models.gateway.menu.cancel')}</Button>
            <Button type="button" onClick={save} disabled={!dirty || menuWrite.pending}>
              {menuWrite.pending && <LoaderCircle className="animate-spin" aria-hidden="true" />}
              {t(menuWrite.pending ? 'settings.models.gateway.menu.saving' : 'settings.models.gateway.menu.save')}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
