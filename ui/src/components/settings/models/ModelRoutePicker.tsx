import * as React from 'react';
import { Check, ChevronDown, Plus, Search } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { modelSupplierCounts, orderedRouteSources } from './modelRows';
import type { AgentSupply, Source } from './types';

type RouteTarget = {
  source: Source;
  modelId: string;
  displayName: string | null;
  manual: boolean;
};

export const ModelRoutePicker: React.FC<{
  agent: AgentSupply;
  sources: Source[];
  value: string;
  servedBy?: string | null;
  disabled?: boolean;
  onChange: (modelId: string) => void;
  onAddModel: () => void;
}> = ({ agent, sources, value, servedBy, disabled, onChange, onAddModel }) => {
  const { t } = useTranslation();
  const backendName = t(`settings.models.backends.${agent.backend}`, { defaultValue: agent.backend }) as string;
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState('');
  const routeSources = React.useMemo(() => orderedRouteSources(agent, sources), [agent, sources]);
  const groups = React.useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return routeSources
      .map((source) => ({
        source,
        targets: source.models
          .map(
            (model): RouteTarget => ({
              source,
              modelId: model.id,
              displayName: model.display_name ?? null,
              manual: model.provenance === 'manual',
            }),
          )
          .filter((target) => {
            if (!needle) return true;
            return [source.display_name, target.modelId, target.displayName ?? '']
              .some((part) => part.toLocaleLowerCase().includes(needle));
          }),
      }))
      .filter((group) => group.targets.length > 0);
  }, [query, routeSources]);

  const selectedSources = React.useMemo(
    () => routeSources.filter((source) => source.models.some((model) => model.id === value)),
    [routeSources, value],
  );
  const supplierCountByModel = React.useMemo(() => modelSupplierCounts(routeSources), [routeSources]);
  const selectedLabel = selectedSources.length === 0
    ? value
    : servedBy
      ? `${value} · ${servedBy}`
      : selectedSources.length === 1
      ? `${value} · ${selectedSources[0].display_name}`
      : t('settings.models.routes.multipleSources', { model: value, count: selectedSources.length }) as string;

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (disabled) return;
        setOpen(next);
        if (!next) setQuery('');
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          className="flex min-h-11 w-full items-center gap-2 rounded-lg border border-border bg-background px-3 text-left text-[12.5px] transition-colors hover:border-border-strong disabled:opacity-60"
        >
          <span className={cn('min-w-0 flex-1 truncate', value ? 'font-medium text-foreground' : 'text-muted')}>
            {value ? selectedLabel : t('settings.models.routes.chooseTarget')}
          </span>
          <ChevronDown className="size-4 shrink-0 text-muted" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        sideOffset={6}
        className="flex max-h-[420px] w-[min(420px,var(--radix-popover-trigger-width))] flex-col overflow-hidden p-0"
      >
        <div className="relative border-b border-border p-2.5">
          <Search className="pointer-events-none absolute left-5 top-1/2 size-3.5 -translate-y-1/2 text-muted" />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('settings.models.routes.searchPlaceholder') as string}
            className="h-9 pl-8 text-[12.5px]"
            autoFocus
          />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
          {groups.length === 0 ? (
            <p className="px-3 py-8 text-center text-[12.5px] text-muted">{t('settings.models.routes.empty')}</p>
          ) : (
            groups.map(({ source, targets }) => (
              <div key={source.id} className="pb-2 last:pb-0">
                <div className="sticky top-0 z-[1] flex items-center justify-between gap-2 bg-popover px-2.5 py-2">
                  <span className="truncate text-[11px] font-bold text-foreground">{source.display_name}</span>
                  <span className="shrink-0 text-[10.5px] text-muted">{t('settings.models.routes.availableCount', { count: targets.length })}</span>
                </div>
                {targets.map((target) => (
                  <button
                    key={`${source.id}:${target.modelId}`}
                    type="button"
                    onClick={() => {
                      onChange(target.modelId);
                      setOpen(false);
                    }}
                    className={cn(
                      'flex min-h-11 w-full items-center gap-2 rounded-md px-2.5 py-2 text-left transition-colors hover:bg-surface-2',
                      value === target.modelId && 'bg-mint-soft/60',
                    )}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block break-all font-mono text-[12.5px] font-medium text-foreground">
                        {target.modelId}
                      </span>
                      {target.displayName && (
                        <span className="block truncate text-[11px] text-muted">{target.displayName}</span>
                      )}
                      {(supplierCountByModel.get(target.modelId) ?? 0) > 1 && (
                        <span className="block text-[10.5px] text-muted">
                          {t('settings.models.routes.orderDecidesSource', { backend: backendName })}
                        </span>
                      )}
                    </span>
                    {target.manual && (
                      <Badge variant="secondary" className="shrink-0 px-1.5 py-0 text-[9.5px]">
                        {t('settings.models.routes.manualBadge')}
                      </Badge>
                    )}
                    {value === target.modelId && <Check className="size-4 shrink-0 text-mint" />}
                  </button>
                ))}
              </div>
            ))
          )}
        </div>
        <button
          type="button"
          onClick={() => {
            setOpen(false);
            onAddModel();
          }}
          className="flex min-h-11 items-center gap-2 border-t border-border px-3.5 py-2.5 text-left text-[12.5px] font-semibold text-mint transition-colors hover:bg-surface-2"
        >
          <Plus className="size-4" />
          {t('settings.models.routes.addManual')}
        </button>
      </PopoverContent>
    </Popover>
  );
};
