import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, RefreshCw } from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { useApi } from '../../../context/ApiContext';
import type { MemoryItemsResult } from '../../../context/ApiContext';
import { useMemoryResource } from './useMemoryResource';

type MemoryItemsOk = Extract<MemoryItemsResult, { status: 'ok' }>;

export const MemoryProfilePanel: React.FC<{ enabled: boolean }> = ({ enabled }) => {
  const { t } = useTranslation();
  const api = useApi();
  // Only a SUCCESSFUL response is the benign Provider-A case: `profile_warning:'empty'`
  // (or simply zero items) renders as the graceful "not available"/empty copy. A closed
  // failure — sidecar down, provider outage, timeout, etc. — is a real ERROR, and the
  // resource surfaces it distinctly per its code.
  const { data, error, loading, reload } = useMemoryResource<MemoryItemsOk>({
    read: api.getMemoryProfile,
    failureMessageKey: 'memory.profile.loadFailed',
    enabled,
    // Refresh is an explicit gesture: report the new attempt, not the old failure.
    clearErrorOnReload: true,
    resetDataOnError: true,
  });

  useEffect(() => {
    void reload();
  }, [reload]);

  if (!enabled) {
    return <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">{t('memory.profile.disabledHint')}</div>;
  }

  const items = data?.items ?? null;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[12.5px] text-muted">{t('memory.profile.description')}</p>
        <Button variant="ghost" size="sm" onClick={() => void reload()} disabled={loading}>
          {loading ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
          {t('memory.profile.refresh')}
        </Button>
      </div>
      {error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">{error}</div>
      ) : loading && items === null ? (
        <div className="flex items-center gap-2 px-1 text-sm text-muted">
          <Loader2 className="size-4 animate-spin" />
          {t('memory.profile.loading')}
        </div>
      ) : data?.profile_warning === 'empty' ? (
        <div className="rounded-2xl border border-gold/30 bg-gold/[0.06] p-6 text-center text-[13px] text-foreground">
          {t('memory.profile.warningUnavailable')}
        </div>
      ) : !items || items.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">
          {t('memory.profile.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item, idx) => (
            // Inert text nodes only; never use Markdown/HTML rendering for provider content.
            <div key={idx} className="rounded-xl border border-border bg-surface px-4 py-3">
              <div className="mb-1 flex items-center gap-2">
                <Badge variant="secondary">{t(`memory.kind.${item.kind}`)}</Badge>
                {item.date ? <span className="font-mono text-[10.5px] text-muted">{item.date}</span> : null}
              </div>
              <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">{item.text}</p>
            </div>
          ))}
          <p className="px-1 text-[11px] text-muted">{t('memory.profile.sourceNote')}</p>
        </div>
      )}
    </div>
  );
};
