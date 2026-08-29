import * as React from 'react';
import { LoaderCircle, RefreshCw, Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import type { PendingWrite } from './asyncLifetime';
import type { CollectionReadAuthority } from './collectionReadAuthority';
import { eligibleSources } from './eligibility';
import {
  applyOpenCodeMenuIntent,
  openCodeMenuIntent,
  readOpenCodeMenuBaseline,
  sameOpenCodeMenu,
  type OpenCodeMenuBaseline,
} from './menuBaseline';
import { buildIdentifier } from './menus/identifiers';
import { modelsApi } from './modelsApi';
import type { AgentMenu, AgentSupply, Source } from './types';

type MenuModel = {
  id: string;
  sourceNames: string[];
};

const selectableModels = (agent: AgentSupply, sources: Source[]): MenuModel[] => {
  const standardVendors = new Set(agent.standard_vendors ?? []);
  const namesById = new Map<string, Set<string>>();
  const sourceNames = new Map(sources.map((source) => [source.id, source.display_name]));
  for (const source of eligibleSources(sources, agent)) {
    for (const model of source.models) {
      if (model.retired) continue;
      const id = buildIdentifier(source.vendor, model.id, standardVendors);
      const names = namesById.get(id) ?? new Set<string>();
      names.add(source.display_name);
      namesById.set(id, names);
    }
  }
  for (const id of agent.menu?.checked ?? []) {
    if (namesById.has(id)) continue;
    const names = new Set<string>();
    for (const hop of agent.routes?.[id]?.hops ?? []) {
      const name = sourceNames.get(hop.source_id);
      if (name) names.add(name);
    }
    namesById.set(id, names);
  }
  return [...namesById.entries()]
    .map(([id, names]) => ({ id, sourceNames: [...names].sort((left, right) => left.localeCompare(right)) }))
    .sort((left, right) => left.id.localeCompare(right.id));
};

export const OpenCodeMenuDialog: React.FC<{
  open: boolean;
  sourceReads: Pick<CollectionReadAuthority<Source[]>, 'readValue'>;
  onClose: () => void;
  onSaved: (echoed: AgentSupply) => void | Promise<void>;
  onObserved: (observed: AgentSupply) => void | Promise<void>;
  menuWrite: PendingWrite;
}> = ({ open, sourceReads, onClose, onSaved, onObserved, menuWrite }) => {
  const { t } = useTranslation();
  const [baseline, setBaseline] = React.useState<OpenCodeMenuBaseline | null>(null);
  const baselineRef = React.useRef<OpenCodeMenuBaseline | null>(null);
  const [readState, setReadState] = React.useState<'loading' | 'ready' | 'error'>('loading');
  const [selected, setSelectedState] = React.useState<Set<string>>(new Set());
  const selectedRef = React.useRef<Set<string>>(new Set());
  const readAttempt = React.useRef(0);
  const models = React.useMemo(
    () => baseline ? selectableModels(baseline.agent, baseline.sources) : [],
    [baseline],
  );
  const rawChecked = React.useMemo(() => baseline?.agent.menu?.checked ?? [], [baseline]);
  const [query, setQuery] = React.useState('');
  const [saveFailed, setSaveFailed] = React.useState(false);

  const applyBaseline = React.useCallback((next: OpenCodeMenuBaseline, checked: readonly string[]) => {
    const nextSelected = new Set(checked);
    baselineRef.current = next;
    selectedRef.current = nextSelected;
    setBaseline(next);
    setSelectedState(nextSelected);
    setReadState('ready');
  }, []);

  const loadBaseline = React.useCallback(async (preserveDraft: boolean) => {
    const attempt = ++readAttempt.current;
    const previous = baselineRef.current;
    const intent = preserveDraft && previous
      ? openCodeMenuIntent(previous.agent.menu?.checked ?? [], [...selectedRef.current])
      : null;
    setReadState('loading');
    try {
      const next = await readOpenCodeMenuBaseline(modelsApi, sourceReads);
      if (attempt !== readAttempt.current) return;
      const selectableIds = new Set(selectableModels(next.agent, next.sources).map((model) => model.id));
      applyBaseline(
        next,
        intent
          ? applyOpenCodeMenuIntent(next.agent.menu?.checked ?? [], intent, selectableIds)
          : next.agent.menu?.checked ?? [],
      );
      setQuery('');
      setSaveFailed(false);
    } catch {
      if (attempt === readAttempt.current) setReadState('error');
    }
  }, [applyBaseline, sourceReads]);

  React.useEffect(() => {
    if (open) void loadBaseline(false);
    else readAttempt.current += 1;
  }, [loadBaseline, open]);

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleModels = normalizedQuery
    ? models.filter((model) => `${model.id} ${model.sourceNames.join(' ')}`.toLocaleLowerCase().includes(normalizedQuery))
    : models;
  const savedSet = new Set(rawChecked);
  const dirty = selected.size !== savedSet.size || [...selected].some((id) => !savedSet.has(id));
  const baselineReady = readState === 'ready' && baseline !== null;

  const toggle = (id: string) => {
    if (!baselineReady || menuWrite.pending) return;
    setSaveFailed(false);
    const next = new Set(selectedRef.current);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedRef.current = next;
    setSelectedState(next);
  };

  const save = () => {
    const current = baselineRef.current;
    if (!baselineReady || !current || !dirty || menuWrite.pending) return;
    const intent = openCodeMenuIntent(
      current.agent.menu?.checked ?? [],
      [...selectedRef.current],
    );
    setSaveFailed(false);
    void menuWrite.track(async () => {
      let latest: OpenCodeMenuBaseline;
      try {
        latest = await readOpenCodeMenuBaseline(modelsApi, sourceReads);
      } catch {
        setReadState('error');
        setSaveFailed(true);
        return;
      }

      const latestSelectable = new Set(selectableModels(latest.agent, latest.sources).map((model) => model.id));
      const attemptedMenu: AgentMenu = {
        view: latest.agent.menu?.view ?? 'featured',
        checked: applyOpenCodeMenuIntent(latest.agent.menu?.checked ?? [], intent, latestSelectable),
      };
      applyBaseline(latest, attemptedMenu.checked);
      if (sameOpenCodeMenu(latest.agent.menu, attemptedMenu)) {
        await Promise.resolve(onObserved(latest.agent)).catch(() => {});
        onClose();
        return;
      }

      let echoed: AgentSupply;
      try {
        echoed = await modelsApi.putMenu(attemptedMenu);
      } catch {
        try {
          const observed = await readOpenCodeMenuBaseline(modelsApi, sourceReads);
          if (sameOpenCodeMenu(observed.agent.menu, attemptedMenu)) {
            applyBaseline(observed, observed.agent.menu?.checked ?? []);
            await Promise.resolve(onSaved(observed.agent)).catch(() => {});
            onClose();
            return;
          }
          const observedSelectable = new Set(selectableModels(observed.agent, observed.sources).map((model) => model.id));
          applyBaseline(
            observed,
            applyOpenCodeMenuIntent(observed.agent.menu?.checked ?? [], intent, observedSelectable),
          );
          await Promise.resolve(onObserved(observed.agent)).catch(() => {});
        } catch {
          setReadState('error');
        }
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
              disabled={!baselineReady || menuWrite.pending}
            />
          </div>
          <span className="shrink-0 text-[11px] font-semibold text-muted">
            {baselineReady
              ? t('settings.models.gateway.menu.selected', { selected: selected.size, total: models.length })
              : t('settings.models.gateway.selectedModelCount', { count: selected.size })}
          </span>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto rounded-md border border-border bg-background p-1.5">
          {!baselineReady ? (
            <div className="flex flex-col items-center gap-3 px-4 py-10 text-center text-[12.5px] text-muted">
              {readState === 'loading' && <LoaderCircle className="size-4 animate-spin" aria-hidden="true" />}
              <p>{t('settings.models.gateway.menu.baselineUnavailable')}</p>
              {readState === 'error' && <Button type="button" variant="outline" size="xs" onClick={() => void loadBaseline(true)}><RefreshCw aria-hidden="true" />{t('settings.models.gateway.retry')}</Button>}
            </div>
          ) : models.length === 0 ? (
            <p className="px-4 py-10 text-center text-[12.5px] text-muted">{t('settings.models.gateway.menu.empty')}</p>
          ) : visibleModels.length === 0 ? (
            <p className="px-4 py-10 text-center text-[12.5px] text-muted">{t('settings.models.gateway.menu.noMatch')}</p>
          ) : (
            <div className="space-y-1">
              {visibleModels.map((model) => {
                const checked = selected.has(model.id);
                const sourceLabel = model.sourceNames.length > 0
                  ? model.sourceNames.join(' · ')
                  : t('settings.models.gateway.menu.configured');
                return (
                  <button
                    key={model.id}
                    type="button"
                    role="checkbox"
                    aria-checked={checked}
                    disabled={!baselineReady || menuWrite.pending}
                    onClick={() => toggle(model.id)}
                    className={cn(
                      'flex min-h-11 w-full min-w-0 items-center gap-3 rounded-md px-3 py-2 text-left transition-colors hover:bg-surface-2 disabled:opacity-50',
                      checked && 'bg-surface-2',
                    )}
                  >
                    <Checkbox checked={checked} presentational />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-[12px] font-medium text-foreground" title={model.id}>{model.id}</span>
                      <span className="mt-0.5 block truncate text-[10.5px] text-muted" title={sourceLabel}>{sourceLabel}</span>
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
            <Button type="button" onClick={save} disabled={!baselineReady || !dirty || menuWrite.pending}>
              {menuWrite.pending && <LoaderCircle className="animate-spin" aria-hidden="true" />}
              {t(menuWrite.pending ? 'settings.models.gateway.menu.saving' : 'settings.models.gateway.menu.save')}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
