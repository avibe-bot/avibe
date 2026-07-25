import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Search as SearchIcon } from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Input } from '../../ui/input';
import { useApi } from '../../../context/ApiContext';
import type { MemoryItemsResult } from '../../../context/ApiContext';
import { useMemoryResource } from './useMemoryResource';

type MemoryItemsOk = Extract<MemoryItemsResult, { status: 'ok' }>;

export const MemorySearchPanel: React.FC<{ enabled: boolean }> = ({ enabled }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [query, setQuery] = useState('');
  const read = useCallback((text: string) => api.searchMemory(text, 20), [api]);
  const {
    data,
    error,
    loading: searching,
    loaded: searched,
    reload: search,
  } = useMemoryResource<MemoryItemsOk, [string]>({
    read,
    failureMessageKey: 'memory.search.searchFailed',
  });

  const runSearch = () => {
    const trimmed = query.trim();
    if (!trimmed) return;
    void search(trimmed);
  };

  if (!enabled) {
    return <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">{t('memory.search.disabledHint')}</div>;
  }

  const items = data?.items ?? null;

  return (
    <div className="flex flex-col gap-3">
      <p className="text-[12.5px] text-muted">{t('memory.search.description')}</p>
      <div className="flex gap-2">
        <div className="relative flex-1">
          <SearchIcon size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') runSearch();
            }}
            placeholder={t('memory.search.placeholder')}
            className="pl-9 text-[13px]"
          />
        </div>
        <Button onClick={runSearch} disabled={searching || !query.trim()}>
          {searching ? <Loader2 className="size-3.5 animate-spin" /> : null}
          {searching ? t('memory.search.searching') : t('memory.search.button')}
        </Button>
      </div>
      {error ? (
        <div className="rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">{error}</div>
      ) : !searched ? null : !items || items.length === 0 ? (
        <div className="rounded-2xl border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">
          {t('memory.search.empty')}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item, idx) => (
            <div key={idx} className="rounded-xl border border-border bg-surface px-4 py-3">
              <div className="mb-1 flex items-center gap-2">
                <Badge variant="secondary">{t(`memory.kind.${item.kind}`)}</Badge>
                {item.date ? <span className="font-mono text-[10.5px] text-muted">{item.date}</span> : null}
              </div>
              <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">{item.text}</p>
            </div>
          ))}
          <p className="px-1 text-[11px] text-muted">{t('memory.search.sourceNote')}</p>
        </div>
      )}
    </div>
  );
};
