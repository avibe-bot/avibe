import * as React from 'react';
import { LoaderCircle, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import { SourceRow } from './SourceRow';
import type { AdoptedBy, Source } from './types';

export const SourcesCard: React.FC<{
  sources: Source[];
  readState: 'loading' | 'ready' | 'error';
  onRetry: () => void;
  onOpenSource: (source: Source) => void;
  onAddApiKey: () => void;
  adoptionBySource?: Readonly<Record<string, readonly AdoptedBy[]>>;
}> = ({ sources, readState, onRetry, onOpenSource, onAddApiKey, adoptionBySource = {} }) => {
  const { t } = useTranslation();
  const groups = [
    { id: 'native', sources: sources.filter((source) => source.supply_channel === 'native_cli') },
    { id: 'hub', sources: sources.filter((source) => source.supply_channel === 'hub') },
  ].filter((group) => group.sources.length > 0);
  return (
    <section className="relative z-20 flex max-h-full min-h-[420px] flex-col self-start overflow-hidden rounded-[14px] border border-border bg-surface">
      <div className="flex h-14 shrink-0 items-center justify-between border-b border-border px-3.5">
        <span className="flex items-center gap-[7px]">
          <h2 className="text-[16px] font-bold text-foreground">{t('settings.models.upstream.heading')}</h2>
          <ModelHubInfoHint
            label={t('settings.models.shell.gatewayInfo.label')}
            content={t('settings.models.shell.gatewayInfo.body')}
            className="model-hub-upstream-info"
          />
        </span>
        {readState === 'ready' && <span className="rounded-full border border-border bg-foreground/[0.04] px-2 py-[3px] text-[10.5px] font-semibold text-muted">{t('settings.models.upstream.count', { count: sources.length })}</span>}
      </div>
      <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto p-3">
        {readState === 'loading'
          ? <div className="flex h-full min-h-36 items-center justify-center"><LoaderCircle className="size-4 animate-spin text-muted" /></div>
          : readState === 'error'
            ? <div className="flex h-full min-h-36 flex-col items-center justify-center gap-3 px-4 text-center"><p className="text-[12px] text-muted">{t('settings.models.upstream.unread')}</p><Button variant="outline" size="xs" onClick={onRetry}>{t('settings.models.upstream.retry')}</Button></div>
            : groups.length > 0
              ? groups.map((group) => <div key={group.id} className="space-y-2"><h3 className="model-hub-upstream-group-label flex h-[18px] items-center uppercase">{t(`settings.models.upstream.group.${group.id}`)}</h3>{group.sources.map((source) => <SourceRow key={source.id} source={source} adoptedBy={adoptionBySource[source.id]} onOpen={onOpenSource} />)}</div>)
              : <p className="px-3 py-10 text-center text-[12px] text-muted">{t('settings.models.upstream.empty')}</p>}
      </div>
      <div className="flex h-14 shrink-0 items-center gap-2 border-t border-border px-3.5">
        {/* G-21: the frame supplies no vendor for the per-vendor subscription
            dialog. Keep the drawn command visible without inventing a picker or
            a default vendor. */}
        <Button
          variant="default"
          size="xs"
          disabled
          className="model-hub-footer-action shadow-none disabled:opacity-100"
        >
          <Plus className="size-3" />
          {t('settings.models.upstream.addSubscription')}
        </Button>
        <Button
          variant="outline"
          size="xs"
          className="model-hub-footer-action"
          onClick={onAddApiKey}
        >
          <Plus className="size-3" />
          {t('settings.models.upstream.addApiKey')}
        </Button>
      </div>
    </section>
  );
};
