import * as React from 'react';
import { LoaderCircle, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { ModelHubInfoHint } from './ModelHubInfoHint';
import { foldRegionRead, type RegionRead } from './regionRead';
import { SourceRow } from './SourceRow';
import type { Source } from './types';

export const SourcesCard: React.FC<{
  read: RegionRead<Source[]>;
  onRetry: () => void;
  readFailureCopy?: string;
  onOpenSource: (source: Source) => void;
  onAddApiKey: () => void;
  onAddSubscription: () => void;
  subscriptionPickerOpen?: boolean;
  subscriptionTriggerRef?: React.Ref<HTMLButtonElement>;
}> = ({ read, onRetry, readFailureCopy, onOpenSource, onAddApiKey, onAddSubscription, subscriptionPickerOpen, subscriptionTriggerRef }) => {
  const { t } = useTranslation();
  const sources = foldRegionRead<Source[], Source[] | undefined>(read, {
    loading: () => undefined,
    ready: (data) => data,
    unread: () => undefined,
    degraded: (staleData) => staleData,
  });
  const groups = [
    { id: 'native', sources: (sources ?? []).filter((source) => source.supply_channel === 'native_cli') },
    { id: 'hub', sources: (sources ?? []).filter((source) => source.supply_channel === 'hub') },
  ].filter((group) => group.sources.length > 0);
  // No height floor: the frame sizes this panel to its cards, so a fixture with
  // fewer sources than the design draws should end below the footer rather than
  // leave ~100px of void above it. `max-h-full` still hands overflow to the
  // scroll region when there are more.
  return (
    <section className="relative z-20 flex max-h-full flex-col self-start overflow-hidden rounded-[14px] border border-border bg-surface">
      <div className="flex h-14 shrink-0 items-center justify-between border-b border-border px-3.5">
        <span className="flex items-center gap-[7px]">
          <h2 className="text-[16px] font-bold leading-[23px] text-foreground">{t('settings.models.upstream.heading')}</h2>
          <ModelHubInfoHint
            label={t('settings.models.shell.gatewayInfo.label')}
            content={t('settings.models.shell.gatewayInfo.body')}
            className="model-hub-upstream-info"
          />
        </span>
        {sources !== undefined && <span className="model-hub-pill model-hub-upstream-count border">{t('settings.models.upstream.count', { count: sources.length })}</span>}
      </div>
      <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto p-3">
        {read.kind === 'loading' && sources === undefined
          ? <div className="flex h-full min-h-36 items-center justify-center"><LoaderCircle className="size-4 animate-spin text-muted" /></div>
          : read.kind === 'unread'
            ? <div className="flex h-full min-h-36 flex-col items-center justify-center gap-3 px-4 text-center"><p className="text-[12px] text-muted">{t('settings.models.upstream.unread')}</p><Button variant="outline" size="xs" onClick={onRetry}>{t('settings.models.upstream.retry')}</Button></div>
            : <>
                {read.kind === 'degraded' && read.cause === 'read_failed' && <div className="mb-2 flex items-center justify-between gap-3 rounded-lg border border-destructive/25 bg-destructive/[0.08] px-3 py-2"><p className="text-[11px] text-destructive-ink">{readFailureCopy ?? t('settings.models.upstream.unread')}</p><Button variant="outline" size="xs" onClick={onRetry}>{t('settings.models.upstream.retry')}</Button></div>}
                {groups.length > 0
                  ? groups.map((group) => <div key={group.id} className="space-y-2.5"><h3 className="model-hub-upstream-group-label flex h-[18px] items-center uppercase">{t(`settings.models.upstream.group.${group.id}`)}</h3>{group.sources.map((source) => <SourceRow key={source.id} source={source} onOpen={onOpenSource} />)}</div>)
                  : <p className="px-3 py-10 text-center text-[12px] text-muted">{t('settings.models.upstream.empty')}</p>}
              </>}
      </div>
      <div className="flex h-14 shrink-0 items-center gap-2 border-t border-border px-3.5">
        <Button
          ref={subscriptionTriggerRef}
          variant="default"
          size="xs"
          aria-haspopup="menu"
          aria-expanded={subscriptionPickerOpen}
          className="model-hub-footer-action model-hub-footer-action--filled shadow-none disabled:opacity-100"
          onClick={onAddSubscription}
        >
          <Plus className="size-3" />
          {t('settings.models.upstream.addSubscription')}
        </Button>
        <Button
          variant="outline"
          size="xs"
          className="model-hub-footer-action model-hub-footer-action--outlined model-hub-fill-0a"
          onClick={onAddApiKey}
        >
          <Plus className="size-3" />
          {t('settings.models.upstream.addApiKey')}
        </Button>
      </div>
    </section>
  );
};
